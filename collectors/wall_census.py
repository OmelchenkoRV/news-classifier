"""
Daily wall census — do futures books carry fewer walls than spot, and is
any given day typical?

WHY
---
On 2026-10-10 the heatmap showed clear spot walls and few futures walls. A
live check found futures books much flatter near price, but that was one
weekend morning. This service repeats that check every 5 minutes, the same
way each time, so after a few weeks each day can be compared with the rest.

WHAT IS MEASURED (fixed 2026-10-10, before any census data)
-----------------------------------------------------------
Every INTERVAL (default 300 s), REST snapshots of both books for ETHUSDT and
BTCUSDT: spot (5,000 levels) and USD-M futures (1,000 levels).

1. COMMON ZONE — where both books are fully visible:
     ETH  +-0.35% of mid, in $0.5 slots;   BTC  +-0.10% of mid, in $5 slots.
   The futures snapshot is 1,000 levels; even with every tick occupied it
   reaches $10 (ETH, 0.40% at $2,500) and $100 (BTC, 0.12% at $82,000), so
   these zones fit inside it with room to spare. A row is `complete` only
   if the snapshot reaches the zone edge on both sides.
   Per row: slot count; median, 90th-percentile and largest slot; share of
   the zone's USD in the top 5% of slots; bid and ask USD; and
     n3 / n5 = slots at least 3x / 5x the median of their occupied
               neighbours (+-10 slots, same side, at least 5 neighbours).
   No dollar minimum: this compares the SHAPE of the two books.

2. FAR ZONE, SPOT ONLY — where the visible spot walls sit:
     0.5% to 3% from mid (capped at the snapshot's reach), $2 / $50 slots,
     the heatmap's wall rule: at least $250k (ETH) / $1M (BTC) and 3x the
     median occupied slot within +-0.25% (at least 5 slots each side).
   Futures cannot be measured here: the public futures snapshot stops at
   about +-0.45% / +-0.16%.

Descriptive monitoring, not a hypothesis test. Nothing here feeds the
pre-registered wall tests (scripts/wall_fate.py, scripts/wall_fate_stream.py).

USAGE
-----
    python -m collectors.wall_census --loop            # service (every 5 min)
    python -m collectors.wall_census --once            # one snapshot set, printed
    python -m collectors.wall_census --report          # daily table
    python -m collectors.wall_census --self-test       # no network, no DB
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("wall_census")

CODE_VERSION = "2026-10-10"
INTERVAL = int(os.getenv("CENSUS_INTERVAL", "300"))
SYMBOLS = ("ETHUSDT", "BTCUSDT")
MARKETS = {
    "spot":    dict(urls=[os.getenv("SPOT_REST", "https://api.binance.com") + "/api/v3/depth",
                          "https://data-api.binance.vision/api/v3/depth"], limit=5000),
    "futures": dict(urls=[os.getenv("FUT_REST", "https://fapi.binance.com") + "/fapi/v1/depth"],
                    limit=1000),
}
ZONE = {"ETHUSDT": 0.35, "BTCUSDT": 0.10}          # common zone, % of mid
FINE = {"ETHUSDT": 0.5, "BTCUSDT": 5.0}            # common-zone slot width, USD
K_NEIGH, MIN_NEIGH = 10, 5
FAR_LO, FAR_HI = 0.5, 3.0                          # spot far zone, % of mid
COARSE = {"ETHUSDT": 2.0, "BTCUSDT": 50.0}         # same slots as the heatmap
FAR_MIN = {"ETHUSDT": 250e3, "BTCUSDT": 1e6}       # heatmap's spot wall minimum
FAR_LOCAL_PCT, FAR_X = 0.25, 3.0

DDL = """
CREATE TABLE IF NOT EXISTS wall_census (
    ts TIMESTAMPTZ NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
    mid DOUBLE PRECISION, reach_bid REAL, reach_ask REAL,
    zone_pct REAL, fine_width REAL, complete BOOLEAN,
    slots INT, median_usd DOUBLE PRECISION, p90_usd DOUBLE PRECISION,
    max_usd DOUBLE PRECISION, top5_share REAL, zone_usd DOUBLE PRECISION,
    bid_usd DOUBLE PRECISION, ask_usd DOUBLE PRECISION, n3 INT, n5 INT,
    far_lo REAL, far_hi_bid REAL, far_hi_ask REAL, far_bid INT, far_ask INT,
    far_top TEXT, code_version TEXT,
    PRIMARY KEY (ts, market, symbol));
"""
COLS = ("ts", "market", "symbol", "mid", "reach_bid", "reach_ask", "zone_pct",
        "fine_width", "complete", "slots", "median_usd", "p90_usd", "max_usd",
        "top5_share", "zone_usd", "bid_usd", "ask_usd", "n3", "n5", "far_lo",
        "far_hi_bid", "far_hi_ask", "far_bid", "far_ask", "far_top", "code_version")


# ------------------------------------------------------------------ measures
def _slot(price: float, width: float) -> int:
    return math.floor(price / width + 1e-9)


def slots_in(levels: list, mid: float, lo_pct: float, hi_pct: float, width: float) -> dict:
    """{slot: USD} for levels whose distance from mid is within [lo_pct, hi_pct] %."""
    out: dict = {}
    for p, q in levels:
        d = abs(p / mid - 1) * 100
        if lo_pct <= d <= hi_pct:
            k = _slot(p, width)
            out[k] = out.get(k, 0.0) + p * q
    return out


def _median(v: list) -> float:
    s = sorted(v)
    return s[len(s) // 2] if s else float("nan")


def standouts(slots: dict, k: int, x: float, min_neigh: int = MIN_NEIGH) -> list:
    """[(slot, usd, ratio)] for slots >= x times the median of occupied
    neighbours within +-k slots (excluding itself; at least min_neigh)."""
    out = []
    for s, v in slots.items():
        nb = [slots[j] for j in range(s - k, s + k + 1) if j != s and slots.get(j, 0) > 0]
        if len(nb) < min_neigh:
            continue
        med = _median(nb)
        if med > 0 and v >= x * med:
            out.append((s, v, v / med))
    return out


def common_zone(bids: list, asks: list, mid: float, zone: float, width: float) -> dict:
    sb = slots_in(bids, mid, 0.0, zone, width)
    sa = slots_in(asks, mid, 0.0, zone, width)
    vals = sorted(list(sb.values()) + list(sa.values()))
    n = len(vals)
    if not n:
        return dict(slots=0)
    tot = sum(vals)
    s3 = standouts(sb, K_NEIGH, 3.0) + standouts(sa, K_NEIGH, 3.0)
    return dict(slots=n, median_usd=vals[n // 2], p90_usd=vals[int(n * 0.9)],
                max_usd=vals[-1], top5_share=sum(vals[int(n * 0.95):]) / tot,
                zone_usd=tot, bid_usd=sum(sb.values()), ask_usd=sum(sa.values()),
                n3=len(s3), n5=sum(1 for _, _, r in s3 if r >= 5.0))


def far_walls(levels: list, mid: float, hi_pct: float, width: float, min_usd: float) -> list:
    """Spot far-zone walls on one side: [(price, usd, ratio)], biggest first."""
    if hi_pct <= FAR_LO:
        return []
    # neighbours come from a slightly wider band so edge slots are judged fairly
    sl = slots_in(levels, mid, max(0.0, FAR_LO - FAR_LOCAL_PCT), hi_pct, width)
    k = max(5, round(FAR_LOCAL_PCT / 100 * mid / width))
    out = []
    for s, v, r in standouts(sl, k, FAR_X):
        d = abs((s + 0.5) * width / mid - 1) * 100
        if v >= min_usd and FAR_LO <= d <= hi_pct:
            out.append((s * width, v, r))
    return sorted(out, key=lambda t: -t[1])


def measure(market: str, symbol: str, book: dict, ts: datetime) -> tuple:
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    mid = (bids[0][0] + asks[0][0]) / 2
    reach_b = (1 - bids[-1][0] / mid) * 100
    reach_a = (asks[-1][0] / mid - 1) * 100
    zone, width = ZONE[symbol], FINE[symbol]
    cz = common_zone(bids, asks, mid, zone, width)
    complete = reach_b >= zone and reach_a >= zone and cz.get("slots", 0) > 0
    far = dict(far_lo=None, far_hi_bid=None, far_hi_ask=None, far_bid=None,
               far_ask=None, far_top=None)
    if market == "spot":
        hb, ha = min(FAR_HI, reach_b), min(FAR_HI, reach_a)
        fb = far_walls(bids, mid, hb, COARSE[symbol], FAR_MIN[symbol])
        fa = far_walls(asks, mid, ha, COARSE[symbol], FAR_MIN[symbol])
        top = sorted(fb + fa, key=lambda t: -t[1])[:4]
        far = dict(far_lo=FAR_LO, far_hi_bid=round(hb, 3), far_hi_ask=round(ha, 3),
                   far_bid=len(fb), far_ask=len(fa),
                   far_top=json.dumps([[p, round(v), round(r, 1)] for p, v, r in top]))
    row = dict(ts=ts, market=market, symbol=symbol, mid=mid,
               reach_bid=round(reach_b, 3), reach_ask=round(reach_a, 3),
               zone_pct=zone, fine_width=width, complete=complete,
               slots=cz.get("slots"), median_usd=cz.get("median_usd"),
               p90_usd=cz.get("p90_usd"), max_usd=cz.get("max_usd"),
               top5_share=cz.get("top5_share"), zone_usd=cz.get("zone_usd"),
               bid_usd=cz.get("bid_usd"), ask_usd=cz.get("ask_usd"),
               n3=cz.get("n3"), n5=cz.get("n5"), **far, code_version=CODE_VERSION)
    return tuple(row[c] for c in COLS)


# ------------------------------------------------------------------ I/O
def fetch(market: str, symbol: str) -> dict:
    import requests
    cfg = MARKETS[market]
    err = None
    for url in cfg["urls"]:
        try:
            r = requests.get(url, params={"symbol": symbol, "limit": cfg["limit"]}, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                   # noqa: BLE001
            err = e
    raise RuntimeError(f"{market} {symbol}: {err}")


def snapshot_set(ts: datetime) -> list:
    rows = []
    for symbol in SYMBOLS:
        for market in MARKETS:
            try:
                rows.append(measure(market, symbol, fetch(market, symbol), ts))
            except Exception as e:                               # noqa: BLE001
                logger.warning("skipped %s %s: %s", market, symbol, e)
    return rows


def _conn():
    from config.database import get_connection
    conn = get_connection()
    conn.cursor().execute(DDL)
    conn.commit()
    return conn


def store(rows: list) -> None:
    if not rows:
        return
    from psycopg2.extras import execute_values
    conn = _conn()
    try:
        execute_values(conn.cursor(),
                       f"INSERT INTO wall_census ({', '.join(COLS)}) VALUES %s "
                       "ON CONFLICT DO NOTHING", rows)
        conn.commit()
    finally:
        conn.close()


def _line(r: tuple) -> str:
    d = dict(zip(COLS, r))
    s = (f"{d['symbol'][:3]} {d['market']:<7} mid {d['mid']:,.2f} "
         f"zone ±{d['zone_pct']}% {'complete' if d['complete'] else 'INCOMPLETE'}: "
         f"{d['n3']} slots ≥3x ({d['n5']} ≥5x), p90/median "
         f"{(d['p90_usd'] or 0) / (d['median_usd'] or 1):.1f}")
    if d["market"] == "spot":
        s += f"; far walls bid {d['far_bid']} ask {d['far_ask']}"
    return s


def loop() -> None:
    logger.info("wall census every %d s (code %s)", INTERVAL, CODE_VERSION)
    while True:
        now = time.time()
        time.sleep(INTERVAL - now % INTERVAL)                    # align to the clock
        ts = datetime.fromtimestamp(round(time.time() / INTERVAL) * INTERVAL, tz=timezone.utc)
        rows = snapshot_set(ts)
        try:
            store(rows)
        except Exception as e:                                   # noqa: BLE001
            logger.warning("store failed (%d rows lost): %s", len(rows), e)
            continue
        logger.info("%s  %s", ts.strftime("%H:%M"), " | ".join(_line(r) for r in rows))


# ------------------------------------------------------------------ report
def report(days: int = 60) -> int:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT (ts AT TIME ZONE 'UTC')::date AS d, market, symbol, count(*),
               avg(n3), avg(n5), avg(p90_usd / NULLIF(median_usd, 0)), avg(top5_share),
               avg(far_bid + far_ask), avg(zone_usd)
        FROM wall_census
        WHERE complete AND ts >= now() - %s::interval
        GROUP BY 1, 2, 3 ORDER BY 1""", (f"{days} days",))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("No census rows yet — is the wall-census service running?")
        return 1
    data: dict = {}
    for d, m, s, n, n3, n5, pr, t5, far, zu in rows:
        data.setdefault(s, {}).setdefault(d, {})[m] = dict(
            n=n, n3=float(n3), n5=float(n5), pr=float(pr or 0), t5=float(t5 or 0),
            far=None if far is None else float(far), zu=float(zu or 0))
    print("Wall census — averages per snapshot over each UTC day (complete snapshots only)")
    print("Common zone: ETH ±0.35% in $0.5 slots, BTC ±0.10% in $5 slots; "
          "≥3x = slots at least 3x their neighbours.")
    print("Far: spot walls 0.5–3% from mid (heatmap rule). * = weekend. "
          "Today's row is partial until the day ends.\n")
    today = datetime.now(timezone.utc).date()
    for s in SYMBOLS:
        if s not in data:
            continue
        print(f"{s}")
        print(f"  {'day':<13}{'snaps':>11}{'≥3x spot':>10}{'≥3x fut':>9}"
              f"{'p90/med spot':>14}{'p90/med fut':>13}{'far walls spot':>16}")
        for d in sorted(data[s]):
            sp, fu = data[s][d].get("spot"), data[s][d].get("futures")
            g = lambda x, k, f="{:.1f}": "–" if not x or x.get(k) is None else f.format(x[k])
            star = "*" if d.weekday() >= 5 else " "
            part = " (today)" if d == today else ""
            print(f"  {d}{star:<3}{(sp or {}).get('n', 0):>5}/{(fu or {}).get('n', 0):<5}"
                  f"{g(sp, 'n3'):>10}{g(fu, 'n3'):>9}{g(sp, 'pr'):>14}{g(fu, 'pr'):>13}"
                  f"{g(sp, 'far'):>16}{part}")
        full = [d for d in sorted(data[s]) if d != today]
        if len(full) >= 7:
            last = full[-1]
            print(f"  {last} against the {len(full) - 1} earlier full days "
                  "(rank 1 = fewest):")
            for m in ("spot", "futures"):
                vals = [data[s][d][m]["n3"] for d in full if m in data[s][d]]
                if m in data[s][last] and len(vals) >= 7:
                    v = data[s][last][m]["n3"]
                    rank = 1 + sum(1 for x in vals if x < v)
                    print(f"    {m:<8} ≥3x slots {v:.1f}: rank {rank} of {len(vals)} "
                          f"(median day {_median(vals):.1f})")
        else:
            print(f"  ({len(full)} full days so far; a comparison is printed from 7.)")
        print()
    return 0


# ------------------------------------------------------------------ self-test
def self_test() -> int:
    ts = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)

    def book(mid, width, n, size_usd, spikes=None, reach=None):
        """Symmetric book: one level per slot, size_usd each; spikes {price: usd}."""
        spikes = spikes or {}
        bids, asks = [], []
        for i in range(1, n + 1):
            for side, p in (("b", mid - i * width), ("a", mid + i * width)):
                if reach is not None and abs(p / mid - 1) * 100 > reach:
                    continue
                usd = spikes.get(round(p, 6), size_usd)
                (bids if side == "b" else asks).append([f"{p:.2f}", f"{usd / p:.10f}"])
        return {"bids": bids, "asks": asks}

    # flat futures-like book: nothing stands out
    b = book(2500.25, 0.5, 400, 1e6)
    r = dict(zip(COLS, measure("futures", "ETHUSDT", b, ts)))
    assert r["complete"] and r["n3"] == 0 and r["n5"] == 0, r
    assert abs(r["p90_usd"] / r["median_usd"] - 1) < 1e-6
    print("  [ok] flat book: 0 slots at 3x, p90/median 1.0, complete")

    # spiky spot-like book: three 6x slots and one 4x slot inside the zone
    sp = {round(2500.25 - 3 * 0.5, 6): 6e5, round(2500.25 - 9 * 0.5, 6): 6e5,
          round(2500.25 + 5 * 0.5, 6): 6e5, round(2500.25 + 12 * 0.5, 6): 4e5}
    b = book(2500.25, 0.5, 4000, 1e5, spikes=sp)
    r = dict(zip(COLS, measure("spot", "ETHUSDT", b, ts)))
    assert (r["n3"], r["n5"]) == (4, 3), (r["n3"], r["n5"])
    assert r["far_bid"] == 0 and r["far_ask"] == 0, r
    print("  [ok] spiky book: 4 slots at 3x, 3 of them at 5x")

    # futures snapshot that stops short of the zone -> incomplete
    b = book(2500.25, 0.5, 400, 1e6, reach=0.30)
    r = dict(zip(COLS, measure("futures", "ETHUSDT", b, ts)))
    assert not r["complete"], r
    print("  [ok] snapshot reaching only 0.30% < zone 0.35% -> incomplete")

    # far spot walls: base $100k per $2 slot ($0.5 levels at $25k).
    # 2,450 slot $450k (4.5x) counts; 2,550 slot $280k (2.8x) does not
    mid = 2500.25
    spikes = {round(2450.25, 6): 375e3, round(2550.25, 6): 205e3}
    b = book(mid, 0.5, 6000, 25e3, spikes=spikes)
    r = dict(zip(COLS, measure("spot", "ETHUSDT", b, ts)))
    top = json.loads(r["far_top"])
    assert r["far_bid"] == 1 and r["far_ask"] == 0, (r["far_bid"], r["far_ask"], top)
    assert top[0][0] == 2450.0 and top[0][1] == 450_000 and top[0][2] == 4.5, top
    # thinner book: a 5x slot of $200k is below the $250k minimum
    b = book(mid, 0.5, 6000, 10e3, spikes={round(2450.25, 6): 170e3})
    r = dict(zip(COLS, measure("spot", "ETHUSDT", b, ts)))
    assert r["far_bid"] == 0, json.loads(r["far_top"])
    print("  [ok] far spot walls: $450k at 2,450 (4.5x) counted; 2.8x not; "
          "5x but under $250k not")

    # BTC spot reaching only 1.1%: far zone capped at the reach
    b = book(82000.5, 10.0, 90, 2e5)
    r = dict(zip(COLS, measure("spot", "BTCUSDT", b, ts)))
    assert r["far_hi_bid"] <= 1.11 and r["far_hi_ask"] <= 1.11, r
    print("  [ok] far zone capped at the snapshot's reach (BTC spot ~1.1%)")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily wall census, spot vs futures")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.report:
        return report(a.days)
    if a.once:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
        rows = snapshot_set(ts)
        for r in rows:
            print(_line(r))
        store(rows)
        return 0
    if a.loop:
        loop()
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
