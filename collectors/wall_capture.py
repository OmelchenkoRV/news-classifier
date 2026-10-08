"""
Wall capture — Binance spot order book on a FIXED price grid, every minute.

WHY
---
On 2026-10-08 ETH fell through $2,500. The capture's 1%-depth series showed
large bids appearing and vanishing three times without price trading into
them, and bid depth at its thinnest just before the break. That suggests
walls are pulled before price arrives — but eth_orderbook_depth stores
depth by DISTANCE FROM MID and stops at ~2%, so it cannot follow one wall
over time. This collector stores where size sits by ABSOLUTE price, so a
wall at $2,400 is the same bucket in every snapshot, and stores 1-minute
high/low so the analysis knows whether price reached a wall before it
vanished. Analysis: scripts/wall_fate.py (pre-registered there and in
docs/PLAN_wall_fate.md).

WHAT IS STORED
--------------
ob_book    one row per symbol per snapshot (default every 60 s):
           mid, best bid/ask, bucket width, and two REAL[] arrays of USD
           notional per bucket on a fixed grid:
             bids[i] = USD resting in bucket id (bid_base - i)
             asks[i] = USD resting in bucket id (ask_base + i)
           bucket id = floor(price / width); width ETH $2, BTC $50.
           Arrays cover the whole book the API returns (5,000 levels,
           ~4% each side). reach_bid / reach_ask record how far it went.
ob_minutes 1-minute klines (open/high/low/close/volume), closed minutes only.

Storage: ~1 KB per snapshot -> ~3 MB/day for two symbols (~1 GB/year).
API weight: depth(limit=5000) = 250; two symbols per minute = 500 of the
6,000/minute allowance.

USAGE
-----
    python -m collectors.wall_capture --self-test
    python -m collectors.wall_capture --once
    python -m collectors.wall_capture --loop          # the container
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("wall_capture")

WIDTHS = {"ETHUSDT": 2.0, "BTCUSDT": 50.0}       # USD per bucket
BASES = ("https://api.binance.com", "https://data-api.binance.vision")
DEPTH_LIMIT = 5000

DDL = """
CREATE TABLE IF NOT EXISTS ob_book (
    symbol TEXT NOT NULL, captured_at TIMESTAMPTZ NOT NULL,
    mid DOUBLE PRECISION NOT NULL, best_bid DOUBLE PRECISION NOT NULL,
    best_ask DOUBLE PRECISION NOT NULL, width DOUBLE PRECISION NOT NULL,
    bid_base BIGINT NOT NULL, ask_base BIGINT NOT NULL,
    bids REAL[] NOT NULL, asks REAL[] NOT NULL,
    reach_bid DOUBLE PRECISION, reach_ask DOUBLE PRECISION,
    PRIMARY KEY (symbol, captured_at));
CREATE TABLE IF NOT EXISTS ob_minutes (
    symbol TEXT NOT NULL, minute TIMESTAMPTZ NOT NULL,
    open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
    close DOUBLE PRECISION, volume DOUBLE PRECISION,
    PRIMARY KEY (symbol, minute));
"""


def to_grid(bids: list, asks: list, width: float) -> dict:
    """Bucket (price, qty) levels onto the absolute grid id=floor(p/width).
    Prices are exact multiples of the tick, so a tiny epsilon guards against
    float error at bucket edges (2400.0/2.0 must land in 1200, not 1199)."""
    eps = 1e-9
    bid_base = math.floor(bids[0][0] / width + eps)
    ask_base = math.floor(asks[0][0] / width + eps)
    b = [0.0] * (bid_base - math.floor(bids[-1][0] / width + eps) + 1)
    a = [0.0] * (math.floor(asks[-1][0] / width + eps) - ask_base + 1)
    for p, q in bids:
        b[bid_base - math.floor(p / width + eps)] += p * q
    for p, q in asks:
        a[math.floor(p / width + eps) - ask_base] += p * q
    mid = (bids[0][0] + asks[0][0]) / 2
    return {"mid": mid, "best_bid": bids[0][0], "best_ask": asks[0][0],
            "bid_base": bid_base, "ask_base": ask_base, "bids": b, "asks": a,
            "reach_bid": (mid - bids[-1][0]) / mid * 100,
            "reach_ask": (asks[-1][0] - mid) / mid * 100}


def _get(path: str, params: dict):
    import requests
    errors = []
    for base in BASES:
        try:
            r = requests.get(base + path, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                # noqa: BLE001
            errors.append(f"{base}: {e}")
    raise RuntimeError(" | ".join(errors))


def fetch_book(symbol: str):
    d = _get("/api/v3/depth", {"symbol": symbol, "limit": DEPTH_LIMIT})
    bids = [(float(p), float(q)) for p, q in d["bids"]]
    asks = [(float(p), float(q)) for p, q in d["asks"]]
    if not bids or not asks:
        raise ValueError("empty book")
    return bids, asks


def fetch_minutes(symbol: str, start_ms: int) -> list[tuple]:
    now_ms = int(time.time() * 1000)
    data = _get("/api/v3/klines", {"symbol": symbol, "interval": "1m",
                                   "startTime": start_ms, "limit": 1000})
    out = []
    for k in data:
        if int(k[6]) < now_ms:                                # closed only
            out.append((symbol, datetime.fromtimestamp(int(k[0]) / 1000,
                                                       tz=timezone.utc),
                         float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                         float(k[5])))
    return out


def capture_once(conn) -> None:
    from psycopg2.extras import execute_values
    cur = conn.cursor()
    for sym, width in WIDTHS.items():
        try:
            t = datetime.now(timezone.utc)
            g = to_grid(*fetch_book(sym), width)
            cur.execute("""INSERT INTO ob_book (symbol, captured_at, mid, best_bid,
                           best_ask, width, bid_base, ask_base, bids, asks,
                           reach_bid, reach_ask)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT DO NOTHING""",
                        (sym, t, g["mid"], g["best_bid"], g["best_ask"], width,
                         g["bid_base"], g["ask_base"], g["bids"], g["asks"],
                         g["reach_bid"], g["reach_ask"]))
            cur.execute("SELECT max(minute) FROM ob_minutes WHERE symbol = %s",
                        (sym,))
            last = cur.fetchone()[0]
            start_ms = (int(last.timestamp() * 1000) + 60_000 if last
                        else int(time.time() * 1000) - 10 * 60_000)
            rows = fetch_minutes(sym, start_ms)
            if rows:
                execute_values(cur, """INSERT INTO ob_minutes VALUES %s
                                       ON CONFLICT DO NOTHING""", rows)
            conn.commit()
        except Exception as e:                                # noqa: BLE001
            conn.rollback()
            logger.warning("%s: capture failed: %s", sym, e)


def loop(interval: int) -> None:
    from config.database import get_connection
    logger.info("wall capture started: %s every %ds", ", ".join(WIDTHS), interval)
    conn, n = None, 0
    while True:
        try:
            if conn is None or conn.closed:
                conn = get_connection()
                conn.cursor().execute(DDL)
                conn.commit()
            capture_once(conn)
            n += 1
            if n % 60 == 1:
                logger.info("alive: %d cycles", n)
        except Exception as e:                                # noqa: BLE001
            logger.exception("cycle failed: %s", e)
            try:
                conn.close()
            except Exception:                                 # noqa: BLE001
                pass
            conn = None
        # align to the interval boundary (+3 s so the minute candle has closed)
        now = time.time()
        time.sleep(interval - (now % interval) + 3)


def self_test() -> int:
    bids = [(2400.0, 10.0), (2399.0, 1.0), (2398.5, 2.0), (2390.0, 100.0)]
    asks = [(2400.5, 1.0), (2401.99, 1.0), (2402.0, 3.0), (2410.0, 5.0)]
    g = to_grid(bids, asks, 2.0)
    assert g["bid_base"] == 1200 and g["ask_base"] == 1200
    assert g["bids"][0] == 2400.0 * 10                     # [2400, 2402)
    assert abs(g["bids"][1] - (2399.0 + 2398.5 * 2)) < 1e-9  # [2398, 2400)
    assert g["bids"][5] == 2390.0 * 100 and len(g["bids"]) == 6
    assert abs(g["asks"][0] - (2400.5 + 2401.99)) < 1e-9     # [2400, 2402)
    assert g["asks"][1] == 2402.0 * 3 and g["asks"][5] == 2410.0 * 5
    assert abs(g["mid"] - 2400.25) < 1e-12
    print("  [ok] absolute grid: bucket edges exact (2400.0 -> bucket 1200), "
          "bids descend, asks ascend")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Order-book wall capture")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int,
                    default=int(os.getenv("WALL_INTERVAL_SECONDS", "60")))
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.loop:
        loop(a.interval)
        return 0
    from config.database import get_connection
    conn = get_connection()
    try:
        conn.cursor().execute(DDL)
        conn.commit()
        capture_once(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
