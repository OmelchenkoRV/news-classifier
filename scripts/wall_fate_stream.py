"""
Wall fate from the reconstructed book: was each wall TRADED AWAY or CANCELLED?

Reads ws_book_1m (collectors/book_stream.py). Unlike wall_fate.py (minute
photographs), every reduction of a wall here is split exactly into size that
traded and size that was cancelled, for spot AND perpetual futures.

PRE-REGISTRATION (written 2026-10-08, before any stream data was captured)
-------------------------------------------------------------------------
Wall definitions per market (the futures book near price is a dense,
smooth ladder ~6x deeper than spot, so its zones are tighter):
                touch zone  baseline band   wall zone   multiple  min USD (ETH / BTC)
    spot        < 0.25%     0.25 - 2%       0.25 - 3%   5x        $250k / $1M
    futures     < 0.05%     0.05 - 0.5%     0.05 - 3%   5x        $2M / $5M
Baseline = median USD of non-empty buckets in the band, per side, per minute.
Episode: starts when a bucket qualifies; PRESENT while >= 50% of its peak;
ENDS after 2 consecutive minutes below that. CENSORED if a minute in its end
window has a resync, synced_frac < 1, or a capture gap > 3 minutes.

Fate, from flows in the end window (minutes after the last present one, up
to and including the first absent one):
    traded    = min(trd, rem)      cancelled = rem - traded
    FILLED     traded >= 50% of removed
    MOVED      cancelled-dominated, and a bucket within +-3 now holds >= 50%
               of the peak that did not before (re-quoted)
    CANCELLED  otherwise
Also recorded: whether price reached the bucket in the end window (minute
high/low from trades). FILLED without price reaching the bucket would be
impossible — it is checked as a consistency test.

Futures coverage: the local futures book is complete only inside the last
REST snapshot (~+-0.5% ETH, ~+-0.2% BTC); beyond it, only levels that have
changed since are known. PRIMARY futures results use walls inside the
snapshot range. Walls beyond it are reported separately and become primary
only if the deep book passes an adequacy check against Binance's bookDepth
archive (cumulative depth at +-1%; run collectors/bookdepth_backfill.py;
pass = our book holds >= 80% of bookDepth's +-1% notional on >= 80% of
matched minutes).

Questions and prior, on record. Read after >= 28 days, per market, symbol
and side; 95% intervals by bootstrap over calendar days (2,000, seed
20261008).
  S1  Most walls that end are CANCELLED rather than FILLED:
      cancelled / (cancelled + filled) lower bound > 50%.      prior: yes
  S2  Cancellation is likelier as price approaches: per-minute rate of
      CANCELLED endings near vs far, ratio lower bound > 1.
        spot near < 0.5%, far 1-3%;  futures near < 0.1%, far 0.2-0.5%.
                                                               prior: yes
  S3  (descriptive) Of CANCELLED walls, the share pulled BEFORE price
      reached them vs at the touch; share of FILLED walls with traded >
      removed (hidden size replenishing the wall).
NO RESCUE: thresholds are not changed after seeing results.

USAGE
-----
    python -m scripts.wall_fate_stream --self-test
    python -m scripts.wall_fate_stream                       # all markets
    python -m scripts.wall_fate_stream --market futures --days 7
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.wall_fate import day_bootstrap, day_of, _ci  # noqa: E402

PARAMS = {
    "spot":    dict(touch=0.25, base=(0.25, 2.0), zone=(0.25, 3.0), x=5.0,
                    min_usd={"ETHUSDT": 250e3, "BTCUSDT": 1e6},
                    near=(0.0, 0.5), far=(1.0, 3.0)),
    "futures": dict(touch=0.05, base=(0.05, 0.5), zone=(0.05, 3.0), x=5.0,
                    min_usd={"ETHUSDT": 2e6, "BTCUSDT": 5e6},
                    near=(0.0, 0.1), far=(0.2, 0.5)),
}
KEEP_FRAC, MISS_TO_END, MOVE_BUCKETS, GAP_MIN = 0.5, 2, 3, 3
FLOWS = ("rest", "add", "rem", "trd")


@dataclass
class Row:
    t: float
    mid: float
    width: float
    base: int
    arr: dict            # e.g. arr["rest_bid"] -> np.ndarray
    hi: float | None
    lo: float | None
    clean: bool          # no resync, fully synced
    snap_lo: float | None
    snap_hi: float | None

    def get(self, key: str, k: int) -> float | None:
        i = k - self.base
        a = self.arr[key]
        return float(a[i]) if 0 <= i < len(a) else None

    def dist(self, side: str, k: int) -> float:
        if side == "bid":
            return (self.mid - (k + 1) * self.width) / self.mid * 100
        return (k * self.width - self.mid) / self.mid * 100

    def covered(self, side: str, k: int) -> bool:
        if self.snap_lo is None:
            return True
        return (k * self.width >= self.snap_lo) if side == "bid" else \
            ((k + 1) * self.width <= self.snap_hi)


@dataclass
class Ep:
    side: str
    k: int
    start_i: int
    last_i: int
    peak: float
    covered: bool
    miss: int = 0
    absent_i: int | None = None
    fate: str = ""
    reached: bool | None = None
    traded: float = 0.0
    cancelled: float = 0.0
    excess: float = 0.0
    exposures: list = field(default_factory=list)


def candidates(r: Row, side: str, p: dict, min_usd: float) -> dict:
    rest = r.arr[f"rest_{side}"]
    ks = r.base + np.arange(len(rest))
    if side == "bid":
        d = (r.mid - (ks + 1) * r.width) / r.mid * 100
    else:
        d = (ks * r.width - r.mid) / r.mid * 100
    band = (d >= p["base"][0]) & (d <= p["base"][1]) & (rest > 0)
    if band.sum() < 3:
        return {}
    b = float(np.median(rest[band]))
    ok = (d >= p["zone"][0]) & (d <= p["zone"][1]) & (rest >= p["x"] * b) & (rest >= min_usd)
    return {int(k): float(u) for k, u in zip(ks[ok], rest[ok])}


def classify(ep: Ep, rows: list[Row]) -> None:
    win = rows[ep.last_i + 1: ep.absent_i + 1]
    if not win or not all(r.clean for r in win):
        ep.fate = "CENSORED"
        return
    rem = sum(r.get(f"rem_{ep.side}", ep.k) or 0.0 for r in win)
    trd = sum(r.get(f"trd_{ep.side}", ep.k) or 0.0 for r in win)
    if rem <= 0:
        ep.fate = "CENSORED"                       # dropped with no visible removal
        return
    ep.traded, ep.cancelled = min(trd, rem), max(0.0, rem - trd)
    ep.excess = max(0.0, trd - rem)
    top, bot = (ep.k + 1) * rows[0].width, ep.k * rows[0].width
    if ep.side == "bid":
        ep.reached = any(r.lo is not None and r.lo < top for r in win)
    else:
        ep.reached = any(r.hi is not None and r.hi >= bot for r in win)
    if ep.traded >= 0.5 * rem:
        ep.fate = "FILLED"
        return
    a, b = rows[ep.last_i], rows[ep.absent_i]
    for dk in range(-MOVE_BUCKETS, MOVE_BUCKETS + 1):
        if dk:
            nb, was = b.get(f"rest_{ep.side}", ep.k + dk), a.get(f"rest_{ep.side}", ep.k + dk)
            if nb is not None and nb >= KEEP_FRAC * ep.peak and (was is None or was < KEEP_FRAC * ep.peak):
                ep.fate = "MOVED"
                return
    ep.fate = "CANCELLED"


def track(rows: list[Row], market: str, symbol: str) -> list[Ep]:
    p = PARAMS[market]
    min_usd = p["min_usd"].get(symbol, 0.0)
    done, active = [], {}
    for i, r in enumerate(rows):
        if i and r.t - rows[i - 1].t > GAP_MIN * 60 + 30:
            for ep in active.values():
                ep.fate = "CENSORED"; done.append(ep)
            active = {}
        for side in ("bid", "ask"):
            cands = candidates(r, side, p, min_usd)
            for key in [k for k in active if k[0] == side]:
                ep = active[key]
                u = r.get(f"rest_{side}", ep.k)
                if u is None:
                    ep.fate = "CENSORED"; done.append(ep); del active[key]; continue
                if u >= KEEP_FRAC * ep.peak:
                    ep.last_i, ep.miss, ep.absent_i = i, 0, None
                    ep.peak = max(ep.peak, u)
                    ep.exposures.append((i, r.dist(side, ep.k)))
                    continue
                ep.miss += 1
                if ep.miss == 1:
                    ep.absent_i = i
                if ep.miss >= MISS_TO_END:
                    classify(ep, rows); done.append(ep); del active[key]
            for k, u in cands.items():
                if (side, k) not in active:
                    ep = Ep(side, k, i, i, u, r.covered(side, k))
                    ep.exposures.append((i, r.dist(side, k)))
                    active[(side, k)] = ep
    for ep in active.values():
        ep.fate = "ACTIVE"; done.append(ep)
    return done


def tables(eps: list[Ep], rows: list[Row]):
    E = pd.DataFrame([{"side": e.side, "fate": e.fate, "covered": e.covered,
                       "reached": e.reached, "peak": e.peak, "traded": e.traded,
                       "cancelled": e.cancelled, "excess": e.excess,
                       "life_min": (rows[e.last_i].t - rows[e.start_i].t) / 60,
                       "dist_end": rows[e.last_i].dist(e.side, e.k),
                       "day": day_of(rows[e.start_i].t)} for e in eps])
    H = pd.DataFrame([(e.side, d, e.fate == "CANCELLED" and i == e.last_i, e.covered,
                       day_of(rows[i].t))
                      for e in eps if e.fate in ("CANCELLED", "FILLED", "MOVED", "ACTIVE")
                      for i, d in e.exposures],
                     columns=["side", "dist", "event", "covered", "day"])
    return E, H


def cancelled_share(df):
    x = df[df["fate"].isin(["CANCELLED", "FILLED"])]
    return (x["fate"] == "CANCELLED").mean() if len(x) else float("nan")


def ratio_fn(near, far):
    def f(h):
        n = h[(h["dist"] >= near[0]) & (h["dist"] < near[1])]
        m = h[(h["dist"] >= far[0]) & (h["dist"] < far[1])]
        if not len(n) or not len(m) or m["event"].mean() == 0:
            return float("nan")
        return n["event"].mean() / m["event"].mean()
    return f


# ------------------------------------------------------------------ data
def load(market: str, symbol: str, days: int | None) -> list[Row]:
    from config.database import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cond = "AND minute >= now() - %s::interval" if days else ""
        args = (market, symbol, f"{days} days") if days else (market, symbol)
        cur.execute(f"""SELECT minute, mid, width, base, rest_bid, rest_ask, add_bid,
                               add_ask, rem_bid, rem_ask, trd_bid, trd_ask, hi, lo,
                               resyncs, synced_frac, snap_lo, snap_hi
                        FROM ws_book_1m WHERE market = %s AND symbol = %s {cond}
                        ORDER BY minute""", args)
        out = []
        for r in cur.fetchall():
            keys = ("rest_bid", "rest_ask", "add_bid", "add_ask", "rem_bid", "rem_ask",
                    "trd_bid", "trd_ask")
            arr = {k: np.asarray(v, float) for k, v in zip(keys, r[4:12])}
            out.append(Row(r[0].timestamp(), r[1], r[2], int(r[3]), arr, r[12], r[13],
                           (r[14] or 0) == 0 and (r[15] or 0) >= 0.999, r[16], r[17]))
        return out
    finally:
        conn.close()


def report(market: str, symbol: str, rows: list[Row]):
    p = PARAMS[market]
    print(f"\n{'=' * 78}\n{market.upper()} {symbol}: {len(rows)} minutes, "
          f"{day_of(rows[0].t)} … {day_of(rows[-1].t)} "
          f"({len(set(day_of(r.t) for r in rows))} days; "
          f"{sum(not r.clean for r in rows)} minutes with a resync)\n{'=' * 78}")
    E, H = tables(track(rows, market, symbol), rows)
    if E.empty:
        print("  no walls found")
        return
    groups = [("inside snapshot coverage (PRIMARY)", True)]
    if market == "futures":
        groups.append(("beyond snapshot coverage (exploratory until deep-book check)", False))
    for label, cov in groups:
        for side in ("bid", "ask"):
            e = E[(E["side"] == side) & (E["covered"] == cov)]
            if e.empty:
                continue
            c = e["fate"].value_counts()
            print(f"\n  {side.upper()} walls, {label}: {len(e)} — " + ", ".join(
                f"{f} {c.get(f, 0)}" for f in ("CANCELLED", "FILLED", "MOVED", "ACTIVE", "CENSORED")))
            ended = e[e["fate"].isin(["CANCELLED", "FILLED"])]
            if len(ended):
                lo, hi = day_bootstrap(e, cancelled_share)
                print(f"  S1 cancelled share {cancelled_share(e):.0%} of {len(ended)} "
                      f"({_ci(lo, hi, '{:.0%}')}; claim needs lower > 50%)")
                can = e[e["fate"] == "CANCELLED"]
                fil = e[e["fate"] == "FILLED"]
                if len(can):
                    print(f"  S3 cancelled before price arrived {(can['reached'] == False).mean():.0%}, "
                          f"at the touch {(can['reached'] == True).mean():.0%}; median life "
                          f"{can['life_min'].median():.0f} min, last seen "
                          f"{can['dist_end'].median():.2f}% away")
                if len(fil):
                    bad = (fil["reached"] == False).sum()
                    print(f"     filled with hidden size (traded > removed) "
                          f"{(fil['excess'] > 0).mean():.0%}; consistency: {bad} FILLED "
                          f"walls without price reaching them (should be 0)")
            h = H[(H["side"] == side) & (H["covered"] == cov)]
            if len(h):
                f = ratio_fn(p["near"], p["far"])
                lo, hi = day_bootstrap(h, f)
                n = h[(h["dist"] >= p["near"][0]) & (h["dist"] < p["near"][1])]
                m = h[(h["dist"] >= p["far"][0]) & (h["dist"] < p["far"][1])]
                print(f"  S2 cancel-end rate per minute: near {n['event'].mean() if len(n) else float('nan'):.2%} "
                      f"(n={len(n)}), far {m['event'].mean() if len(m) else float('nan'):.2%} "
                      f"(n={len(m)}); ratio {f(h):.2f} ({_ci(lo, hi, '{:.2f}')}; "
                      f"claim needs lower > 1)")


# ------------------------------------------------------------- self-test
def _rows(steps, width=2.0, n=80, base_usd=60_000.0):
    """steps: (mid, {bid_k: rest}, {bid_k: (rem, trd)}, lo, hi, clean)."""
    out = []
    for i, (mid, walls, flows, lo, hi, clean) in enumerate(steps):
        base = math.floor(mid * 0.97 / width)
        arr = {f"{f}_{s}": np.zeros(n) for f in FLOWS for s in ("bid", "ask")}
        top_bid = math.floor((mid - width / 2) / width)
        for k in range(base, base + n):
            if k <= top_bid:
                arr["rest_bid"][k - base] = base_usd
            else:
                arr["rest_ask"][k - base] = base_usd
        for k, u in walls.items():
            arr["rest_bid"][k - base] = u
        for k, (rem, trd) in flows.items():
            arr["rem_bid"][k - base] = rem
            arr["trd_bid"][k - base] = trd
        out.append(Row(1_760_000_000 + 60 * i, mid, width, base, arr, hi, lo, clean, None, None))
    return out


def self_test() -> int:
    k = 1185                     # bucket [2370, 2372): ~1.2% below 2400
    big = 1_000_000.0
    keep = (2400, {k: big}, {}, 2399, 2401, True)
    # cancelled far from price
    rows = _rows([keep] * 3 + [(2400, {}, {k: (big, 0.0)}, 2399, 2401, True),
                               (2400, {}, {}, 2399, 2401, True)])
    e = track(rows, "spot", "ETHUSDT")
    assert [(x.fate, x.reached) for x in e] == [("CANCELLED", False)], [(x.fate, x.reached) for x in e]
    # eaten by trades as price falls into it
    rows = _rows([keep] * 3 + [(2372, {}, {k: (big, 0.9 * big)}, 2369, 2390, True),
                               (2372, {}, {}, 2368, 2374, True)])
    e = track(rows, "spot", "ETHUSDT")
    assert [(x.fate, x.reached) for x in e] == [("FILLED", True)], [(x.fate, x.reached) for x in e]
    # pulled at the touch: price arrived, but mostly cancelled
    rows = _rows([keep] * 3 + [(2373, {}, {k: (big, 0.1 * big)}, 2371, 2390, True),
                               (2373, {}, {}, 2372, 2375, True)])
    e = track(rows, "spot", "ETHUSDT")
    assert [(x.fate, x.reached) for x in e] == [("CANCELLED", True)]
    assert abs(e[0].cancelled - 0.9 * big) < 1e-6 and abs(e[0].traded - 0.1 * big) < 1e-6
    # resync inside the end window -> censored; re-quote -> moved
    rows = _rows([keep] * 3 + [(2400, {}, {k: (big, 0.0)}, 2399, 2401, False),
                               (2400, {}, {}, 2399, 2401, True)])
    assert [x.fate for x in track(rows, "spot", "ETHUSDT")] == ["CENSORED"]
    rows = _rows([keep] * 3 + [(2400, {k - 1: big}, {k: (big, 0.0)}, 2399, 2401, True)] * 3)
    assert sorted(x.fate for x in track(rows, "spot", "ETHUSDT")) == ["ACTIVE", "MOVED"]
    # hidden size: traded more than removed -> FILLED with excess
    rows = _rows([keep] * 3 + [(2372, {}, {k: (big, 1.5 * big)}, 2369, 2390, True),
                               (2372, {}, {}, 2368, 2374, True)])
    e = track(rows, "spot", "ETHUSDT")
    assert e[0].fate == "FILLED" and abs(e[0].excess - 0.5 * big) < 1e-6
    print("  [ok] fates: cancelled far -> CANCELLED (not reached); eaten -> FILLED; "
          "pulled at the touch -> CANCELLED (reached); resync -> CENSORED; "
          "re-quote -> MOVED; hidden size counted")
    # futures parameters find a wall in a dense ladder close to price
    rows = _rows([(2400, {1196: 40e6}, {}, 2399, 2401, True)] * 3, base_usd=6e6)
    e = track(rows, "futures", "ETHUSDT")
    assert len(e) == 1 and e[0].fate == "ACTIVE"
    print("  [ok] futures zones: a 6.7x bucket 0.25% from price is a wall at 6M/bucket density")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Wall fate from the reconstructed book")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--market", choices=list(PARAMS), default=None)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--days", type=int, default=None)
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    for market in ([a.market] if a.market else list(PARAMS)):
        for sym in ([a.symbol] if a.symbol else ["ETHUSDT", "BTCUSDT"]):
            rows = load(market, sym, a.days)
            if len(rows) < 10:
                print(f"{market} {sym}: only {len(rows)} minutes — let book-stream run first")
                continue
            report(market, sym, rows)
    print("\nPre-registered read: after >= 28 days (see docstring).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
