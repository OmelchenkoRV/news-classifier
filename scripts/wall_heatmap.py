"""
Liquidity heatmap of the live rebuilt order books — where the walls sit, how
long they have held, and whether they were PULLED or FILLED.

Reads ws_book_1m (written by collectors/book_stream.py) and writes one
self-contained HTML page:

  - heatmap per market (futures, spot): time across, price up, colour =
    USD resting in each price slot at the end of each minute (log scale);
  - the mid price on top;
  - markers where a wall lost at least half its size in one minute:
      x  PULLED  — the size went without trades taking it (cancelled);
      o  FILLED  — trades took at least half of it;
  - below it, total USD resting on each side within a band around mid
    (+-0.25 / 0.5 / 1 / 2 / 3%, chosen on the page) and the bid/ask ratio,
    with "now" tiles; faded where the band reaches past the snapshot edge;
  - beside it, the book right now (bars), and a "walls now" table:
    slots holding at least the wall minimum and 3x the median occupied slot
    within +-0.25% (at least 5 slots each side).

Snapshot edges: beyond the last REST snapshot (spot 5,000 levels, futures
1,000) the stream only knows levels that have changed since the last sync,
so those slots are lower bounds. The page marks them "partial".

These markers and the table are DESCRIPTIVE. The pre-registered wall tests
are scripts/wall_fate_stream.py (S1-S3); nothing here feeds them.

USAGE
-----
    python -m scripts.wall_heatmap                   # last 6 h, opens the page
    python -m scripts.wall_heatmap --hours 24 --range 4
    python -m scripts.wall_heatmap --watch 60        # rewrite every 60 s; page reloads
    python -m scripts.wall_heatmap --demo demo.html  # synthetic data, no DB
    python -m scripts.wall_heatmap --self-test       # no DB, no network
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("wall_heatmap")

# Same wall minimums and touch zones as the pre-registered stream analysis.
PARAMS = {
    "spot":    dict(touch=0.25, min_usd={"ETHUSDT": 250e3, "BTCUSDT": 1e6}),
    "futures": dict(touch=0.05, min_usd={"ETHUSDT": 2e6, "BTCUSDT": 5e6}),
}
X_LOCAL = 3.0          # a wall: at least 3x the median occupied slot within +-0.25%
EVENT_FRAC = 0.5       # a pull/fill marker: at least half the wall gone in one column
LOCAL_PCT = 0.25       # neighbourhood half-width, at least 5 slots each side
BANDS = (0.25, 0.5, 1.0, 2.0, 3.0)   # % around mid for bid/ask totals
PLOTLY_SRC = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"
ORDER = [("ETHUSDT", "futures"), ("ETHUSDT", "spot"),
         ("BTCUSDT", "futures"), ("BTCUSDT", "spot")]
ARRS = ("rest_bid", "rest_ask", "add_bid", "add_ask", "rem_bid", "rem_ask",
        "trd_bid", "trd_ask")


def bucket_of(price: float, width: float) -> int:
    return math.floor(price / width + 1e-9)


# ------------------------------------------------------------------- data
def load(hours: float) -> dict:
    """{(market, symbol): [row dict, ...]} for the last `hours`."""
    from config.database import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT market, symbol, minute, mid, width, base, {', '.join(ARRS)},
               resyncs, synced_frac, snap_lo, snap_hi
        FROM ws_book_1m
        WHERE minute >= now() - %s::interval
        ORDER BY market, symbol, minute""", (f"{hours} hours",))
    out: dict = {}
    for r in cur.fetchall():
        d = dict(market=r[0], symbol=r[1], minute=r[2], mid=r[3], width=r[4],
                 base=int(r[5]), resyncs=r[14] or 0, synced=r[15],
                 snap_lo=r[16], snap_hi=r[17])
        for k, v in zip(ARRS, r[6:14]):
            d[k] = np.asarray(v if v is not None else [], dtype=float)
        out.setdefault((r[0], r[1]), []).append(d)
    conn.close()
    return out


def _local_median(rest: np.ndarray, w: int) -> np.ndarray:
    """Median of each slot's +-w OCCUPIED neighbours (same column). Zeros are
    skipped: beyond the snapshot a zero usually means "not seen yet"."""
    from numpy.lib.stride_tricks import sliding_window_view
    pad = np.full((w, rest.shape[1]), np.nan)
    occ = np.where(rest > 0, rest, np.nan)
    win = sliding_window_view(np.vstack([pad, occ, pad]), 2 * w + 1, axis=0)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmedian(win, axis=-1)


def depth_totals(rest_bid: np.ndarray, rest_ask: np.ndarray, centres: np.ndarray,
                 mids: np.ndarray, snap_lo: list, snap_hi: list, band_pct: float):
    """USD resting on each side within +-band_pct of mid, per column.

    A slot counts if its centre is inside the band. `complete` is False where
    the band reaches past the snapshot edge on either side: beyond it only
    levels that changed since the last sync are known, so the total there is
    a lower bound (and the ratio can lean towards whichever side was learned)."""
    lo = mids * (1 - band_pct / 100)
    hi = mids * (1 + band_pct / 100)
    bid = (rest_bid * (centres[:, None] >= lo[None, :])).sum(axis=0)
    ask = (rest_ask * (centres[:, None] <= hi[None, :])).sum(axis=0)
    complete = [bool((sl is None or sl <= l) and (sh is None or sh >= h))
                for sl, sh, l, h in zip(snap_lo, snap_hi, lo, hi)]
    return bid, ask, complete


def build_panel(rows: list, market: str, symbol: str, range_pct: float = 3.0,
                max_cols: int = 480) -> dict:
    """Turn minute rows into the arrays the page draws. Pure function."""
    rows = [r for r in rows if r["mid"]]
    if not rows:
        return {}
    width = float(rows[0]["width"])
    mids = np.array([r["mid"] for r in rows], float)
    b0 = bucket_of(mids.min() * (1 - range_pct / 100), width)
    b1 = bucket_of(mids.max() * (1 + range_pct / 100), width)
    nb, n = b1 - b0 + 1, len(rows)

    raw = {k: np.zeros((nb, n)) for k in ARRS}
    for j, r in enumerate(rows):
        for k in ARRS:
            a = r[k]
            if not len(a):
                continue
            lo, hi = max(r["base"], b0), min(r["base"] + len(a) - 1, b1)
            if lo <= hi:
                raw[k][lo - b0:hi - b0 + 1, j] = np.nan_to_num(a[lo - r["base"]:hi - r["base"] + 1])

    # group minutes into at most max_cols columns: resting = end of group, flows summed
    g = max(1, math.ceil(n / max_cols))
    idx = [list(range(s, min(n, s + g))) for s in range(0, n, g)]
    last = [ix[-1] for ix in idx]
    M = {}
    for k in ARRS:
        if k.startswith("rest"):
            M[k] = raw[k][:, last]
        else:
            M[k] = np.stack([raw[k][:, ix].sum(axis=1) for ix in idx], axis=1)
    rest = M["rest_bid"] + M["rest_ask"]
    add = M["add_bid"] + M["add_ask"]
    rem = M["rem_bid"] + M["rem_ask"]
    trd = M["trd_bid"] + M["trd_ask"]
    pulled = np.maximum(0.0, rem - add - trd)          # net size gone, not traded

    times = [rows[i]["minute"] for i in last]
    mid_c = mids[last]
    prices = (np.arange(nb) + b0) * width               # lower edge of each slot
    w = max(5, round(LOCAL_PCT / 100 * float(np.median(mids)) / width))
    lm = _local_median(rest, w)
    p = PARAMS[market]
    min_usd = p["min_usd"][symbol]

    # pull / fill markers: a wall at column x-1 that lost >= half by column x
    events = []
    traded_cols = trd.sum(axis=0) > 0
    for x in range(1, rest.shape[1]):
        if not traded_cols[x]:
            # no trades recorded in this column (e.g. futures rows before the
            # 2026-10-15 websocket fix): pulled vs filled cannot be told apart
            continue
        prev = rest[:, x - 1]
        wall = (prev >= min_usd) & (prev >= X_LOCAL * lm[:, x - 1])
        for y in np.flatnonzero(wall):
            if trd[y, x] >= EVENT_FRAC * prev[y]:
                kind, amt = "filled", trd[y, x]
            elif pulled[y, x] >= EVENT_FRAC * prev[y]:
                kind, amt = "pulled", pulled[y, x]
            else:
                continue
            events.append(dict(t=_iso(times[x]), p=float(prices[y] + width / 2),
                               usd=round(float(amt)), prev=round(float(prev[y])), kind=kind))

    # walls now (last column), per side
    lx = rest.shape[1] - 1
    mid_now = float(mid_c[-1])
    snap_lo, snap_hi = rows[-1]["snap_lo"], rows[-1]["snap_hi"]
    walls = []
    for side, arr in (("bid", M["rest_bid"]), ("ask", M["rest_ask"])):
        v = arr[:, lx]
        centre = prices + width / 2
        dist = (centre / mid_now - 1) * 100
        ok = ((v >= min_usd) & (v >= X_LOCAL * lm[:, lx]) & (np.abs(dist) >= p["touch"])
              & ((dist < 0) if side == "bid" else (dist > 0)))
        for y in sorted(np.flatnonzero(ok), key=lambda y: -v[y])[:8]:
            # held since: last column where this side's slot was below half today's size
            series = arr[y, :]
            below = np.flatnonzero(series < 0.5 * v[y])
            since = times[below[-1] + 1] if len(below) else times[0]
            partial = ((snap_lo is not None and prices[y] + width <= snap_lo) or
                       (snap_hi is not None and prices[y] >= snap_hi))
            walls.append(dict(side=side, lo=float(prices[y]), hi=float(prices[y] + width),
                              dist=round(float(dist[y]), 2), usd=round(float(v[y])),
                              x=round(float(v[y] / lm[y, lx]), 1) if lm[y, lx] > 0 else None,
                              since=_iso(since), held_from_start=not len(below),
                              partial=bool(partial)))

    centres = prices + width / 2
    s_lo = [rows[i]["snap_lo"] for i in last]
    s_hi = [rows[i]["snap_hi"] for i in last]
    depth = {}
    for b in BANDS:
        if b > range_pct:
            continue
        bt, at, ok = depth_totals(M["rest_bid"], M["rest_ask"], centres, mid_c, s_lo, s_hi, b)
        depth[f"{b:g}"] = dict(bid=np.rint(bt).astype(int).tolist(),
                               ask=np.rint(at).astype(int).tolist(), complete=ok)

    rs = [sum(rows[i]["resyncs"] for i in ix) for ix in idx]
    sy = [min((rows[i]["synced"] if rows[i]["synced"] is not None else 1.0) for i in ix)
          for ix in idx]
    k = lambda a: np.rint(a / 1e3).astype(int).tolist()          # $k integers
    return dict(
        market=market, symbol=symbol, width=width, minutes_per_col=g,
        times=[_iso(t) for t in times], prices=prices.tolist(),
        mid=[round(float(m), 2) for m in mid_c],
        snap_lo=s_lo, snap_hi=s_hi, depth=depth,
        rest_k=k(rest), add_k=k(add), pulled_k=k(pulled), trd_k=k(trd),
        rest_bid_now=np.rint(M["rest_bid"][:, lx]).tolist(),
        rest_ask_now=np.rint(M["rest_ask"][:, lx]).tolist(),
        events=events, walls=walls, min_usd=min_usd,
        no_trade_cols=int((~traded_cols).sum()),
        resyncs=int(sum(rs)), synced_min=round(float(min(sy)), 3),
        latest=_iso(rows[-1]["minute"]), mid_now=round(mid_now, 2))


def _iso(t) -> str:
    if isinstance(t, (int, float)):
        t = datetime.fromtimestamp(t, tz=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------- page
def render(panels: list, hours: float, refresh: int | None, js_src: str) -> str:
    data = json.dumps(dict(panels=panels, hours=hours,
                           generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
                      allow_nan=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    head_refresh = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    if js_src.startswith("inline:"):
        script = "<script>" + Path(js_src[7:]).read_text(encoding="utf-8") + "</script>"
    else:
        script = f'<script src="{js_src}"></script>'
    return (TEMPLATE.replace("__REFRESH__", head_refresh)
                    .replace("__PLOTLY__", script)
                    .replace("__DATA__", data))


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
__REFRESH__
<title>Order-book walls</title>
__PLOTLY__
<style>
:root{color-scheme:light;
 --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
 --grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
 --s1:#2a78d6;--bid:#1baf7a;--ask:#eb6834;--warn:#fab219}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink2:#c3c2b7;--muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
 --s1:#3987e5;--bid:#199e70;--ask:#d95926}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
 font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1440px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:20px;margin:0 0 4px;font-weight:600}
.sub{color:var(--ink2);margin:0 0 16px}
.filters{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin:0 0 16px}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.seg button{font:inherit;background:var(--surface);color:var(--ink2);border:0;
 padding:6px 14px;cursor:pointer}
.seg button+button{border-left:1px solid var(--border)}
.seg button[aria-pressed=true]{color:var(--ink);font-weight:600;
 box-shadow:inset 0 -2px 0 var(--s1)}
.seg button:hover{background:color-mix(in srgb,var(--ink) 5%,var(--surface))}
.flabel{color:var(--muted);font-size:12px;margin-right:6px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
 padding:16px 16px 8px;margin:0 0 20px}
.card h2{font-size:16px;margin:0;font-weight:600}
.meta{color:var(--ink2);font-size:13px;margin:2px 0 6px}
.warn{color:var(--ink);font-size:13px;margin:4px 0}
.warn b{color:var(--warn)}
.chart{width:100%;height:780px}
.tiles{display:flex;gap:32px;flex-wrap:wrap;margin:10px 0 2px}
.tile .k{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px}
.tile .key{display:inline-block;width:14px;height:3px;border-radius:2px}
.tile .v{font-size:22px;font-weight:600;line-height:1.2}
.tile .s{color:var(--ink2);font-size:12px}
table{border-collapse:collapse;width:100%;margin:8px 0 8px;font-size:13px}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--grid);
 font-variant-numeric:tabular-nums;white-space:nowrap}
th{color:var(--muted);font-weight:500}
th:first-child,td:first-child{text-align:left}
.tables{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:900px){.tables{grid-template-columns:1fr}.chart{height:640px}}
.tcap{font-size:13px;font-weight:600;margin:8px 0 0}
.note{color:var(--muted);font-size:12px;margin:8px 0 0}
.empty{color:var(--muted);font-size:13px;padding:6px 0}
</style></head>
<body><main>
<h1>Order-book walls</h1>
<p class="sub" id="sub"></p>
<div class="filters">
 <span><span class="flabel">Coin</span><span class="seg" id="coin"></span></span>
 <span><span class="flabel">Window</span><span class="seg" id="win"></span></span>
 <span><span class="flabel">Depth band</span><span class="seg" id="band"></span></span>
</div>
<div id="cards"></div>
<p class="note">Colour: USD resting in each price slot at the end of each column (log scale).
Markers: a slot that was a wall (at least the wall minimum and 3&times; the median occupied slot within &plusmn;0.25%, at least 5 slots each side)
and lost at least half its size within one column &mdash; <b>&times; pulled</b> (gone without trades) or
<b>&#9675; filled</b> (taken by trades). Beyond the snapshot edges only levels that changed since the last
sync are known, so sizes there are lower bounds (&ldquo;partial&rdquo;). Totals: USD resting within the
chosen band around mid on each side; ratio = bids &divide; asks (above 1: more resting bids), on a log scale so
2&times; and &frac12;&times; sit equally far from 1. Totals and ratio are faded where the band reaches past a
snapshot edge, because they may be incomplete there. Descriptive only; the
pre-registered tests are in <code>wall_fate_stream.py</code>.</p>
</main>
<script>
const DATA = __DATA__;
const COINS = [["ETHUSDT","ETH"],["BTCUSDT","BTC"]];
const WINS = [["1h",1],["3h",3],["6h",6],["All",null]];
const BANDS = [["±0.25%","0.25"],["±0.5%","0.5"],["±1%","1"],["±2%","2"],["±3%","3"]];
const state = {coin: "ETHUSDT", win: null, band: "1"};
(function readHash(){const h=new URLSearchParams(location.hash.slice(1));
  if(h.get("coin")) state.coin=h.get("coin"); if(h.get("win")) state.win=h.get("win")==="all"?null:+h.get("win");
  if(h.get("band")) state.band=h.get("band");})();
function writeHash(){location.hash=`coin=${state.coin}&win=${state.win??"all"}&band=${state.band}`;}
const hexA = (hex,a) => {let h=hex.trim().slice(1); if(h.length===3) h=[...h].map(c=>c+c).join("");
  const n=parseInt(h,16); return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`;};
const bandLabel = () => `±${state.band}%`;

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;
// sequential blue ramp (100 -> 700); dark mode flips the anchor so "little" recedes
const RAMP = ["#cde2fb","#9ec5f4","#6da7ec","#3987e5","#256abf","#184f95","#0d366b"];
const ramp = () => (dark() ? [...RAMP].reverse() : RAMP).map((c,i,a)=>[i/(a.length-1), c]);
const money = v => v>=1e9?`$${(v/1e9).toFixed(2)}B`:v>=1e6?`$${(v/1e6).toFixed(2)}M`:v>=1e3?`$${(v/1e3).toFixed(0)}k`:`$${Math.round(v)}`;
const px = (p,w) => w>=10 ? p.toLocaleString("en-US",{maximumFractionDigits:0}) : p.toLocaleString("en-US",{minimumFractionDigits:0,maximumFractionDigits:2});

function el(tag, attrs={}, text){const e=document.createElement(tag);
  for(const[k,v] of Object.entries(attrs)) e.setAttribute(k,v); if(text!=null) e.textContent=text; return e;}

function buildFilters(){
  const coin=document.getElementById("coin"), win=document.getElementById("win");
  coin.replaceChildren(...COINS.map(([v,l])=>{const b=el("button",{"aria-pressed":String(state.coin===v)},l);
    b.onclick=()=>{state.coin=v;writeHash();buildFilters();draw();};return b;}));
  win.replaceChildren(...WINS.map(([l,v])=>{const b=el("button",{"aria-pressed":String(state.win===v)},l);
    b.onclick=()=>{state.win=v;writeHash();buildFilters();applyWindow();};return b;}));
  const have=new Set(DATA.panels.flatMap(p=>Object.keys(p.depth)));
  if(!have.has(state.band)) state.band=have.has("1")?"1":[...have][0];
  document.getElementById("band").replaceChildren(...BANDS.filter(([,v])=>have.has(v)).map(([l,v])=>{
    const b=el("button",{"aria-pressed":String(state.band===v)},l);
    b.onclick=()=>{state.band=v;writeHash();buildFilters();draw();};return b;}));
}

function windowRange(P){
  if(state.win==null) return null;
  const end=new Date(P.times.at(-1).replace(" ","T")+"Z"), start=new Date(end-state.win*3600e3);
  const f=d=>d.toISOString().slice(0,16).replace("T"," ");
  return [f(start), f(end)];
}
function applyWindow(){
  for(const P of DATA.panels.filter(p=>p.symbol===state.coin)){
    const id=`c-${P.market}-${P.symbol}`, r=windowRange(P);
    const gd=document.getElementById(id); if(!gd) continue;
    Plotly.relayout(gd, r ? {"xaxis.range":r} : {"xaxis.autorange":true});
  }
}

function panelTraces(P){
  const ink=css("--ink"), surface=css("--surface"), muted=css("--muted");
  const z=P.rest_k.map(row=>row.map(v=>v>0?Math.log10(v*1e3):null));
  const cd=P.rest_k.map((row,y)=>row.map((v,x)=>[v*1e3,P.add_k[y][x]*1e3,P.pulled_k[y][x]*1e3,P.trd_k[y][x]*1e3]));
  const w=P.width, yc=P.prices.map(p=>p+w/2);
  const zmax=Math.max(...z.flat().filter(v=>v!=null)), zmin=Math.max(3, zmax-4);
  const ticks=[]; for(let e=Math.ceil(zmin);e<=Math.floor(zmax);e++) ticks.push(e);
  const slot=P.prices.map(p=>`${px(p,w)}–${px(p+w,w)}`);
  const cdText=cd.map((row,y)=>row.map(c=>[money(c[0]),money(c[1]),money(c[2]),money(c[3]),slot[y]]));
  const heat={type:"heatmap", x:P.times, y:yc, z, customdata:cdText, zmin, zmax,
    colorscale:ramp(), xaxis:"x", yaxis:"y", hoverongaps:false, zsmooth:false,
    colorbar:{orientation:"v", x:0.808, xanchor:"left", y:0.71, yanchor:"middle", len:0.5, thickness:10,
      tickvals:ticks, ticktext:ticks.map(e=>e>=9?`$${10**(e-9)}B`:e>=6?`$${10**(e-6)}M`:`$${10**(e-3)}k`), tickfont:{color:css("--ink2"),size:11},
      title:{text:"resting", side:"top", font:{color:css("--ink2"),size:11}}, outlinewidth:0},
    hovertemplate:"<b>%{customdata[0]}</b> resting<br>%{customdata[4]} · %{x} UTC"+
      "<br>added %{customdata[1]} · pulled %{customdata[2]} · traded %{customdata[3]}<extra></extra>",
    name:"Resting", showlegend:false};
  const halo={type:"scatter", mode:"lines", x:P.times, y:P.mid, xaxis:"x", yaxis:"y",
    line:{color:surface, width:4}, hoverinfo:"skip", showlegend:false};
  const mid={type:"scatter", mode:"lines", x:P.times, y:P.mid, xaxis:"x", yaxis:"y",
    line:{color:ink, width:2}, name:"Mid price", hovertemplate:"mid %{y:,.2f}<extra></extra>"};
  const snap=(k,show)=>({type:"scatter", mode:"lines", x:P.times, y:P[k], xaxis:"x", yaxis:"y",
    line:{color:muted, width:1, shape:"hv"}, connectgaps:false, name:"Snapshot edge",
    legendgroup:"snap", showlegend:show, hoverinfo:"skip"});
  const ev=(kind,name,symbol)=>{const E=P.events.filter(e=>e.kind===kind);
    const open=symbol.endsWith("-open");
    return {type:"scatter", mode:"markers", x:E.length?E.map(e=>e.t):[null], y:E.length?E.map(e=>e.p):[null],
      xaxis:"x", yaxis:"y",
      name, marker:{symbol, color:ink, size:E.map(e=>Math.max(10,Math.min(22,10+6*Math.log10(Math.max(1,e.usd/P.min_usd))))),
        line:{color:open?ink:surface, width:open?2.5:1.5}},
      customdata:E.map(e=>[money(e.usd), money(e.prev)]),
      hovertemplate:`<b>%{customdata[0]}</b> ${kind}<br>of a %{customdata[1]} wall at %{y:,.2f}<br>%{x} UTC<extra></extra>`};};
  // the book now, by side; slots beyond the snapshot edge faded (lower bounds)
  const lo=P.snap_lo.at(-1), hi=P.snap_hi.at(-1);
  const bars=(side,color)=>{const arr=side==="bid"?P.rest_bid_now:P.rest_ask_now, ys=[], xs=[], cs=[], cd=[];
    P.prices.forEach((p,i)=>{const v=arr[i]; if(v>0){const part=(lo!=null && p+w<=lo)||(hi!=null && p>=hi);
      ys.push(p+w/2); xs.push(v); cs.push(part?hexA(color,0.4):color);
      cd.push([money(v), part?" (partial: lower bound)":""]);}});
    return {type:"bar", orientation:"h", x:xs, y:ys, xaxis:"x2", yaxis:"y", marker:{color:cs, line:{width:0}},
      width:w*0.8, name:side==="bid"?"Bids":"Asks", legendgroup:side, showlegend:false, customdata:cd,
      hovertemplate:`<b>%{customdata[0]}</b> ${side}s now at %{y:,.2f}%{customdata[1]}<extra></extra>`};};
  return [heat, halo, mid, snap("snap_lo",true), snap("snap_hi",false),
          ev("pulled","Wall pulled","x"), ev("filled","Wall filled","circle-open"),
          bars("bid",css("--bid")), bars("ask",css("--ask")), ...depthTraces(P)];
}

function depthTraces(P){
  const D=P.depth[state.band]; if(!D) return [];
  const ink=css("--ink"), bidC=css("--bid"), askC=css("--ask"), B=bandLabel();
  const solid=(arr)=>arr.map((v,i)=>D.complete[i]?v:null);
  const note=D.complete.map(c=>c?"":"<br>band passes the snapshot edge: may be incomplete");
  const tot=(side,color)=>{const v=D[side], name=side==="bid"?"Bids":"Asks";
    const cd=v.map((x,i)=>[money(x), note[i]]);
    return [{type:"scatter", mode:"lines", x:P.times, y:v, xaxis:"x3", yaxis:"y3", line:{color:hexA(color,0.35), width:2},
             legendgroup:side, showlegend:false, hoverinfo:"skip"},
            {type:"scatter", mode:"lines", x:P.times, y:solid(v), xaxis:"x3", yaxis:"y3", connectgaps:false,
             line:{color, width:2}, name, legendgroup:side, customdata:cd,
             hovertemplate:`<b>%{customdata[0]}</b> ${name.toLowerCase()} within ${B}<br>%{x} UTC%{customdata[1]}<extra></extra>`}];};
  const r=D.bid.map((b,i)=>b>0&&D.ask[i]>0?b/D.ask[i]:null);
  const one=P.times.map(()=>1);
  const cd=r.map((x,i)=>[x==null?"–":x.toFixed(2), money(D.bid[i]), money(D.ask[i]), note[i]]);
  // shading: above 1 = more resting bids, below 1 = more resting asks; faint where incomplete
  const shade=(vals,fn,color,a)=>[
    {type:"scatter", mode:"lines", x:P.times, y:one, xaxis:"x4", yaxis:"y4", line:{width:0}, hoverinfo:"skip", showlegend:false},
    {type:"scatter", mode:"lines", x:P.times, y:vals.map(x=>x==null?null:fn(x,1)), xaxis:"x4", yaxis:"y4",
     fill:"tonexty", fillcolor:hexA(color,a), line:{width:0}, hoverinfo:"skip", showlegend:false}];
  return [...tot("bid",bidC), ...tot("ask",askC),
    ...shade(r,Math.max,bidC,0.10), ...shade(r,Math.min,askC,0.10),
    ...shade(solid(r),Math.max,bidC,0.28), ...shade(solid(r),Math.min,askC,0.28),
    {type:"scatter", mode:"lines", x:P.times, y:r, xaxis:"x4", yaxis:"y4", line:{color:hexA(ink,0.3), width:1.5},
     hoverinfo:"skip", showlegend:false},
    {type:"scatter", mode:"lines", x:P.times, y:solid(r), xaxis:"x4", yaxis:"y4", connectgaps:false,
     line:{color:ink, width:1.5}, name:"Bid/ask ratio", showlegend:false, customdata:cd,
     hovertemplate:"<b>%{customdata[0]}</b> bid/ask<br>bids %{customdata[1]} · asks %{customdata[2]}<br>%{x} UTC%{customdata[3]}<extra></extra>"}];
}

function panelLayout(P){
  const ink2=css("--ink2"), grid=css("--grid"), axis=css("--axis"), surface=css("--surface");
  const ax={gridcolor:grid, linecolor:axis, zeroline:false, tickfont:{color:ink2,size:11}};
  const r=windowRange(P);
  const B=bandLabel();
  return {paper_bgcolor:surface, plot_bgcolor:surface, margin:{l:72,r:8,t:8,b:44},
    font:{family:'system-ui,-apple-system,"Segoe UI",sans-serif', color:ink2, size:12},
    showlegend:true, legend:{orientation:"h", x:0, y:1.05, font:{size:12,color:ink2}},
    xaxis:{...ax, domain:[0,0.79], anchor:"y", ...(r?{range:r}:{}), showticklabels:false},
    xaxis3:{...ax, domain:[0,0.79], anchor:"y3", matches:"x", showticklabels:false},
    xaxis4:{...ax, domain:[0,0.79], anchor:"y4", matches:"x", title:{text:"UTC", font:{size:11}}},
    xaxis2:{...ax, domain:[0.88,1], anchor:"y", title:{text:"now", font:{size:11}}, tickformat:"~s", tickprefix:"$", nticks:3},
    yaxis:{...ax, domain:[0.42,1], tickformat:",", title:{text:"price", font:{size:11}}},
    yaxis3:{...ax, domain:[0.22,0.38], tickformat:"~s", tickprefix:"$", nticks:4, rangemode:"tozero",
      title:{text:`within ${B}`, font:{size:11}}},
    yaxis4:{...ax, domain:[0,0.18], type:"log", tickvals:[0.25,0.5,1,2,4], ticktext:["¼","½","1","2","4"],
      title:{text:"bid/ask", font:{size:11}}},
    hoverlabel:{bgcolor:surface, bordercolor:axis, font:{color:css("--ink"), size:12}},
    bargap:0, hovermode:"closest"};
}

function wallTable(P, side){
  const W=P.walls.filter(w=>w.side===side);
  const box=el("div"); box.append(el("p",{class:"tcap"}, side==="bid"?"Bid walls now (support)":"Ask walls now (resistance)"));
  if(!W.length){box.append(el("p",{class:"empty"},"None at least the wall minimum and 3× its neighbours."));return box;}
  const t=el("table"), h=el("tr");
  ["Price","From mid","Resting","× neighbours","Holding since","Note"].forEach(c=>h.append(el("th",{},c)));
  t.append(h);
  for(const w of W){const tr=el("tr");
    [`${px(w.lo,P.width)}–${px(w.hi,P.width)}`, `${w.dist>0?"+":""}${w.dist.toFixed(2)}%`, money(w.usd),
     w.x==null?"–":`${w.x}×`, w.held_from_start?"whole window":w.since.slice(11)+" UTC", w.partial?"partial (lower bound)":""]
      .forEach(c=>tr.append(el("td",{},c)));
    t.append(tr);}
  box.append(t); return box;
}

function tiles(P){
  const D=P.depth[state.band], row=el("div",{class:"tiles"}); if(!D) return row;
  const n=D.bid.length-1, B=bandLabel(), part=!D.complete[n];
  const tile=(k,v,s,color)=>{const t=el("div",{class:"tile"}), kk=el("div",{class:"k"});
    if(color){const sw=el("span",{class:"key"}); sw.style.background=color; kk.append(sw);}
    kk.append(document.createTextNode(k)); t.append(kk, el("div",{class:"v"},v)); if(s) t.append(el("div",{class:"s"},s)); return t;};
  const ratio=i=>D.ask[i]>0&&D.bid[i]>0?D.bid[i]/D.ask[i]:null;
  const r=ratio(n);
  // the column closest to one hour before the latest
  const tEnd=new Date(P.times[n].replace(" ","T")+"Z")-3600e3;
  let j=0; P.times.forEach((t,i)=>{if(new Date(t.replace(" ","T")+"Z")<=tEnd) j=i;});
  const r1=ratio(j), was=j<n&&r1!=null?` · 1 h ago: ${r1.toFixed(2)}`:"";
  const lean=r==null?"":r>=1.05?"more resting bids":r<=0.95?"more resting asks":"about balanced";
  const pnote=part?" · may be incomplete (band passes the snapshot edge)":"";
  row.append(tile(`Bids within ${B}`, money(D.bid[n]), null, css("--bid")),
             tile(`Asks within ${B}`, money(D.ask[n]), null, css("--ask")),
             tile("Bid/ask ratio", r==null?"–":r.toFixed(2), lean+was+pnote));
  return row;
}

function draw(){
  const cards=document.getElementById("cards"); cards.replaceChildren();
  const now=Date.now();
  for(const P of DATA.panels.filter(p=>p.symbol===state.coin)){
    const card=el("section",{class:"card"});
    card.append(el("h2",{},`${P.symbol.replace("USDT","")} ${P.market} — resting liquidity`));
    const ev=P.events.reduce((a,e)=>(a[e.kind]++,a),{pulled:0,filled:0});
    const noTr=P.no_trade_cols||0;
    card.append(el("p",{class:"meta"},
      `mid ${px(P.mid_now,P.width)} · latest ${P.latest} UTC · ${P.minutes_per_col} min per column · `+
      `wall minimum ${money(P.min_usd)} · in this window: ${ev.pulled} pulled, ${ev.filled} filled · `+
      `resyncs ${P.resyncs}, lowest synced share ${(P.synced_min*100).toFixed(0)}%`));
    const age=(now-new Date(P.latest.replace(" ","T")+"Z"))/60e3;
    if(noTr>0){const p=el("p",{class:"warn"}); p.append(el("b",{},"⚠ No trades recorded "),
      document.createTextNode(`in ${noTr} of ${P.times.length} columns: no pulled/filled markers there `+
      `(futures before the 2026-10-15 book-stream fix had no trade feed).`)); card.append(p);}
    if(age>5){const p=el("p",{class:"warn"}); p.append(el("b",{},"⚠ Stale: "),
      document.createTextNode(`latest data is ${Math.round(age)} min old — is book-stream running?`)); card.append(p);}
    card.append(tiles(P));
    const div=el("div",{class:"chart", id:`c-${P.market}-${P.symbol}`}); card.append(div);
    const tabs=el("div",{class:"tables"}); tabs.append(wallTable(P,"ask"), wallTable(P,"bid")); card.append(tabs);
    cards.append(card);
    Plotly.newPlot(div, panelTraces(P), panelLayout(P), {responsive:true, displaylogo:false,
      modeBarButtonsToRemove:["lasso2d","select2d"]});
  }
}

document.getElementById("sub").textContent =
  `Binance order books rebuilt live by book-stream · last ${DATA.hours} h · generated ${DATA.generated}`;
if(!DATA.panels.length){document.getElementById("cards").append(el("p",{class:"empty"},
  "No ws_book_1m rows in this window — is book-stream running?"));}
else{ if(!DATA.panels.some(p=>p.symbol===state.coin)) state.coin=DATA.panels[0].symbol;
  buildFilters(); draw(); }
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script></body></html>
"""


# ------------------------------------------------------------------ synthetic
def synthetic(hours: float = 6.0, seed: int = 7, end: datetime | None = None) -> dict:
    """Plausible books with known walls: some pulled as price nears, some filled."""
    rng = np.random.default_rng(seed)
    end = (end or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    n = int(hours * 60)
    t = [end - timedelta(minutes=n - 1 - i) for i in range(n)]
    out = {}
    specs = {"ETHUSDT": (2.0, 2476.0), "BTCUSDT": (50.0, 81600.0)}
    for sym, (wd, p0) in specs.items():
        k = np.arange(n) / max(1, n - 1)
        trend = np.where(k < 0.4, 0.013 * k / 0.4, 0.013 - 0.021 * (k - 0.4) / 0.6)
        noise = np.cumsum(rng.standard_t(4, n) * 0.00015)
        lr = trend + noise - noise[0]
        mid = p0 * np.exp(lr - lr[-1])                                # ends at p0
        for market, depth, reach in (("futures", 9e6, 0.005), ("spot", 9e5, 0.03)):
            scale = depth * (1 if sym == "ETHUSDT" else 2.5)
            top, i70 = float(mid.max()), int(0.7 * n)
            walls = [(top * 1.002, scale * 5, "pull"),             # pulled as the rally peaks
                     (float(mid[i70]) * 0.998, scale * 3, "fill"),  # traded through on the slide
                     (top * 1.012, scale * 6, "hold"),
                     (float(mid.min()) * 0.99, scale * 7, "hold"),
                     (float(mid[0]) * 0.985, scale * 4, "hold")]
            alive = [True] * len(walls)
            rows = []
            for i in range(n):
                m = mid[i]
                b0 = bucket_of(m * 0.94, wd)
                nb = bucket_of(m * 1.06, wd) - b0 + 1
                c = (np.arange(nb) + b0 + 0.5) * wd
                d = np.abs(c / m - 1)
                base = scale * np.exp(-d / 0.004) * (0.6 + 0.8 * rng.random(nb)) + scale * 0.05 * rng.random(nb)
                rb = np.where(c < m, base, 0.0)
                ra = np.where(c > m, base, 0.0)
                add = np.zeros(nb); rem = np.zeros(nb); trd = np.zeros(nb)
                for j, (wp, size, kind) in enumerate(walls):
                    y = bucket_of(wp, wd) - b0
                    if not (0 <= y < nb) or not alive[j]:
                        continue
                    near = abs(wp / m - 1) < 0.003
                    if near and kind == "pull":
                        alive[j] = False; rem[y] += size; continue
                    if near and kind == "fill":
                        alive[j] = False; rem[y] += size; trd[y] += size; continue
                    (rb if wp < m else ra)[y] += size
                trd[int(np.argmin(np.abs(c - m)))] += scale * 0.02   # everyday trading at the touch
                lo_s, hi_s = m * (1 - reach), m * (1 + reach)
                if market == "futures":                 # deep book learned over time
                    beyond = (c < lo_s) | (c > hi_s)
                    slots = np.arange(nb) + b0
                    known = (slots * 2654435761) % 100 < 30 + 60 * i / n
                    rb = np.where(beyond & ~known, 0.0, rb)
                    ra = np.where(beyond & ~known, 0.0, ra)
                rows.append(dict(market=market, symbol=sym, minute=t[i], mid=float(m),
                                 width=wd, base=b0, resyncs=0, synced=1.0,
                                 snap_lo=float(lo_s), snap_hi=float(hi_s),
                                 rest_bid=rb, rest_ask=ra, add_bid=add * 0, add_ask=add,
                                 rem_bid=rem * 0, rem_ask=rem, trd_bid=trd * 0, trd_ask=trd))
            out[(market, sym)] = rows
    return out


# ------------------------------------------------------------------ self-test
def self_test() -> int:
    t0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

    def row(i, mid, rest_bid, rest_ask=None, rem=None, trd=None, base=1200, snap=(None, None)):
        z = np.zeros(len(rest_bid))
        return dict(market="futures", symbol="ETHUSDT", minute=t0 + timedelta(minutes=i),
                    mid=mid, width=2.0, base=base, resyncs=0, synced=1.0,
                    snap_lo=snap[0], snap_hi=snap[1],
                    rest_bid=np.asarray(rest_bid, float),
                    rest_ask=np.asarray(rest_ask if rest_ask is not None else z, float),
                    add_bid=z, add_ask=z,
                    rem_bid=np.asarray(rem if rem is not None else z, float), rem_ask=z,
                    trd_bid=np.asarray(trd if trd is not None else z, float), trd_ask=z)

    # 60 slots of $2 from 2400; a $20M bid wall at slot 10 (2420-2422), others $1M
    nb = 60
    flat = np.full(nb, 1e6)
    wall = flat.copy(); wall[10] = 20e6
    gone = flat.copy()
    rem = np.zeros(nb); rem[10] = 19e6
    rows = [row(i, 2470.0, wall) for i in range(5)]
    rows.append(row(5, 2470.0, gone, rem=rem))                  # no trades at all recorded
    P = build_panel(rows, "futures", "ETHUSDT", range_pct=3.0)
    assert P["events"] == [] and P["no_trade_cols"] == 6, P["events"]
    print("  [ok] a column with no trades recorded gets no pulled/filled marker")
    tiny = np.zeros(nb); tiny[30] = 1e3                         # trades elsewhere in the book
    rows[-1] = row(5, 2470.0, gone, rem=rem, trd=tiny)          # pulled: none traded at 2420
    P = build_panel(rows, "futures", "ETHUSDT", range_pct=3.0)
    y = P["prices"].index(2420.0)
    assert P["rest_k"][y][0] == 20000 and P["rest_k"][y][5] == 1000, "grid mapping"
    assert len(P["events"]) == 1 and P["events"][0]["kind"] == "pulled" \
        and P["events"][0]["p"] == 2421.0 and P["events"][0]["usd"] == 19e6, P["events"]
    print("  [ok] wall mapped to its price slot; a wall gone without trades -> PULLED at 2421")

    trd = rem.copy()
    rows[-1] = row(5, 2470.0, gone, rem=rem, trd=trd)            # same, but traded
    P = build_panel(rows, "futures", "ETHUSDT")
    assert [e["kind"] for e in P["events"]] == ["filled"], P["events"]
    print("  [ok] the same drop taken by trades -> FILLED")

    rows = [row(i, 2470.0, wall) for i in range(6)]
    P = build_panel(rows, "futures", "ETHUSDT")
    W = [w for w in P["walls"] if w["side"] == "bid"]
    assert W and W[0]["lo"] == 2420.0 and W[0]["usd"] == 20e6 and W[0]["x"] == 20.0 \
        and W[0]["held_from_start"], W
    assert not P["events"]
    print("  [ok] walls-now table: 2420-2422, $20M, 20x neighbours, held since start")

    wall2 = flat.copy(); wall2[10] = 20e6; wall2[3] = 9e6
    rows = [row(i, 2470.0, wall2, snap=(2410.0, None)) for i in range(3)]
    P = build_panel(rows, "futures", "ETHUSDT")
    part = {w["lo"]: w["partial"] for w in P["walls"]}
    assert part == {2420.0: False, 2406.0: True}, part
    print("  [ok] a wall beyond the snapshot edge is flagged partial")

    # totals: bids $1M per $2 slot below 2470, asks $2M per slot above
    bids = np.array([1e6 if (1200 + i) * 2 + 2 <= 2470 else 0.0 for i in range(nb)])
    asks = np.array([2e6 if (1200 + i) * 2 >= 2470 else 0.0 for i in range(nb)])
    rows = [row(0, 2470.0, bids, asks, snap=(2400.0, 2519.0)),
            row(1, 2470.0, bids, asks, snap=(2450.0, 2519.0))]
    P = build_panel(rows, "futures", "ETHUSDT")
    d = P["depth"]["1"]       # +-1%: 2445.3 .. 2494.7 -> 12 bid slots, 12 ask slots
    assert d["bid"] == [12_000_000] * 2 and d["ask"] == [24_000_000] * 2, d
    assert d["complete"] == [True, False], d["complete"]
    assert P["depth"]["0.25"]["bid"][0] == 3_000_000, P["depth"]["0.25"]
    print("  [ok] totals within +-1%: bids $12M, asks $24M (ratio 0.5); "
          "flagged incomplete once the band passes the snapshot edge")

    rows = [row(i, 2470.0, wall, rem=np.full(nb, 1.0)) for i in range(10)]
    P = build_panel(rows, "futures", "ETHUSDT", max_cols=4)
    assert P["minutes_per_col"] == 3 and len(P["times"]) == 4
    assert P["times"][0] == "2026-10-08 12:02" and P["times"][-1] == "2026-10-08 12:09"
    print("  [ok] downsampling: 10 minutes -> 4 columns of 3; resting = end of column")

    S = synthetic(hours=2, seed=3)
    panels = [build_panel(S[(m, s)], m, s) for s, m in ORDER]
    html = render(panels, 2, None, PLOTLY_SRC)
    assert "NaN" not in html.split("const DATA = ")[1].split(";\n")[0]
    kinds = {e["kind"] for p in panels for e in p["events"]}
    assert kinds == {"pulled", "filled"}, kinds
    print(f"  [ok] synthetic page renders ({len(html) / 1e6:.1f} MB for 2 h), "
          "with pulled and filled walls found")
    print("SELF-TEST PASSED")
    return 0


# ----------------------------------------------------------------------- main
def write_page(path: Path, data: dict, hours: float, range_pct: float, max_cols: int,
               refresh: int | None, js_src: str) -> Path:
    panels = [build_panel(data[(m, s)], m, s, range_pct, max_cols)
              for s, m in ORDER if data.get((m, s))]
    panels = [p for p in panels if p]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render(panels, hours, refresh, js_src), encoding="utf-8")
    os.replace(tmp, path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--range", type=float, default=3.0, dest="range_pct",
                    help="price range shown, %% beyond the window's low/high mid (default 3)")
    ap.add_argument("--max-cols", type=int, default=480,
                    help="columns at most; longer windows are grouped (default 480)")
    ap.add_argument("--out", default=None, help="output .html (default reports/walls_<UTC>.html)")
    ap.add_argument("--watch", type=int, default=None, metavar="SECONDS",
                    help="rewrite the page every N seconds; the page reloads itself")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--js", default=PLOTLY_SRC,
                    help="plotly.js URL, or inline:<path to plotly.min.js> for offline use")
    ap.add_argument("--demo", metavar="PATH", help="write a page from synthetic data (no DB)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if a.demo:
        p = write_page(Path(a.demo), synthetic(a.hours), a.hours, a.range_pct, a.max_cols,
                       None, a.js)
        logger.info("wrote %s", p)
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    path = Path(a.out or (Path("reports") / (f"walls_{stamp}.html" if not a.watch else "walls_live.html")))
    opened = False
    while True:
        t = time.time()
        data = load(a.hours)
        write_page(path, data, a.hours, a.range_pct, a.max_cols, a.watch, a.js)
        logger.info("wrote %s (%s)", path.resolve(),
                    ", ".join(f"{s[:3]} {m}: {len(data.get((m, s), []))} min" for s, m in ORDER))
        if not opened and not a.no_open:
            webbrowser.open(path.resolve().as_uri())
            opened = True
        if not a.watch:
            return 0
        time.sleep(max(1.0, a.watch - (time.time() - t)))


if __name__ == "__main__":
    sys.exit(main())
