"""
Taker buy/sell flow backfill from the Binance public archive.

WHY
---
A shared CryptoQuant chart flagged the Binance ETH taker buy/sell ratio (7d
MA ≈ 0.95) as "the most bearish level since June". Per this project's
sourcing rule, that is a hypothesis SOURCE, not evidence. This collector
gathers the data to test it properly.

The metric is unusually cheap to test. Binance kline files carry taker-buy
volume alongside total volume (columns 9 and 10), so

    taker_buy_sell_ratio = taker_buy_base / (volume - taker_buy_base)

is reconstructable for every symbol from the public archive — spot back to
2017, USD-M futures back to 2019. No vendor, no 30-day retention wall.

SPOT vs FUTURES
---------------
CryptoQuant's exchange taker ratio is generally derived from DERIVATIVES
flow. Spot and futures taker flow are related but not identical (futures
flow is leverage-driven). Both are collected; futures is the like-for-like
match to the chart, spot is the robustness check. A result that holds on
only one market is a weaker claim.

Kline column layout (identical for spot and um-futures archives):
    0 open_time  1 open  2 high  3 low  4 close  5 volume  6 close_time
    7 quote_volume  8 trades  9 taker_buy_base  10 taker_buy_quote  11 ignore

USAGE
-----
    python -m collectors.taker_flow_backfill --probe
    python -m collectors.taker_flow_backfill
    python -m collectors.taker_flow_backfill --market spot --symbols ETHUSDT
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import zipfile
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("taker_flow_backfill")

ARCHIVE = {
    "spot":    "https://data.binance.vision/data/spot/monthly/klines",
    "futures": "https://data.binance.vision/data/futures/um/monthly/klines",
}
# Earliest months worth requesting; 404s before listing are handled anyway.
START = {"spot": "2017-08", "futures": "2019-09"}

DDL = """
CREATE TABLE IF NOT EXISTS taker_flow (
    id               SERIAL PRIMARY KEY,
    market           TEXT        NOT NULL,     -- 'spot' | 'futures'
    symbol           TEXT        NOT NULL,
    d                DATE        NOT NULL,
    close            DOUBLE PRECISION,
    volume           DOUBLE PRECISION,
    taker_buy_base   DOUBLE PRECISION,
    quote_volume     DOUBLE PRECISION,
    taker_buy_quote  DOUBLE PRECISION,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market, symbol, d)
);
CREATE INDEX IF NOT EXISTS idx_taker_flow_msd
    ON taker_flow (market, symbol, d);
"""


def month_range(start: str, end: str | None = None) -> list[str]:
    y, m = map(int, start.split("-"))
    if end is None:
        now = datetime.now(timezone.utc)
        ey, em = now.year, now.month
    else:
        ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def parse_kline_csv(raw: str) -> list[dict]:
    """Parse one monthly 1d kline CSV, keeping the taker columns."""
    rows = []
    for rec in csv.reader(io.StringIO(raw)):
        if len(rec) < 11:
            continue
        if not rec[0].strip().lstrip("-").isdigit():   # header row
            continue
        try:
            ts_raw = int(rec[0])
            # ms vs microseconds: Binance switched in newer dumps.
            ts = ts_raw / 1_000_000 if ts_raw > 1e14 else ts_raw / 1000
            rows.append({
                "d": datetime.fromtimestamp(ts, tz=timezone.utc).date(),
                "close": float(rec[4]),
                "volume": float(rec[5]),
                "quote_volume": float(rec[7]),
                "taker_buy_base": float(rec[9]),
                "taker_buy_quote": float(rec[10]),
            })
        except (ValueError, OSError, OverflowError):
            continue
    return rows


def fetch_month(market: str, symbol: str, month: str) -> list[dict] | None:
    url = f"{ARCHIVE[market]}/{symbol}/1d/{symbol}-1d-{month}.zip"
    try:
        r = requests.get(url, timeout=60)
    except Exception as e:
        logger.warning("  %s %s %s: %s", market, symbol, month, e)
        return None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        logger.warning("  %s %s %s: HTTP %s", market, symbol, month,
                       r.status_code)
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        raw = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("  %s %s %s: bad zip: %s", market, symbol, month, e)
        return None
    return parse_kline_csv(raw)


def store(market: str, symbol: str, rows: list[dict]) -> int:
    from config.database import get_connection, get_cursor
    from psycopg2.extras import execute_values
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute(DDL)
        conn.commit()
        payload = [(market, symbol, r["d"], r["close"], r["volume"],
                    r["taker_buy_base"], r["quote_volume"],
                    r["taker_buy_quote"]) for r in rows]
        execute_values(cur, """
            INSERT INTO taker_flow
                (market, symbol, d, close, volume, taker_buy_base,
                 quote_volume, taker_buy_quote)
            VALUES %s
            ON CONFLICT (market, symbol, d) DO NOTHING
        """, payload, page_size=1000)
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def run(market: str, symbol: str, probe: bool) -> dict:
    months = month_range(START[market])
    rows, found = [], 0
    for mo in months:
        got = fetch_month(market, symbol, mo)
        if got:
            found += 1
            rows.extend(got)
    if not rows:
        logger.warning("%s %s: no data", market, symbol)
        return {"market": market, "symbol": symbol, "rows": 0}

    first = min(r["d"] for r in rows)
    last = max(r["d"] for r in rows)
    # Sanity: taker buy must lie within [0, volume]. A violation means a
    # column-order mistake, which would silently invert the ratio.
    bad = sum(1 for r in rows
              if r["volume"] > 0 and not (0 <= r["taker_buy_base"] <= r["volume"] * 1.0001))
    logger.info("%s %s: %d rows, %d/%d months, %s … %s, column-sanity "
                "violations: %d", market, symbol, len(rows), found,
                len(months), first, last, bad)
    if bad > len(rows) * 0.01:
        logger.error("  >1%% of rows have taker_buy outside [0, volume] — "
                     "column layout is probably wrong. NOT storing.")
        return {"market": market, "symbol": symbol, "rows": len(rows),
                "error": "column sanity failed"}

    if probe:
        return {"market": market, "symbol": symbol, "rows": len(rows),
                "first": str(first), "last": str(last)}
    n = store(market, symbol, rows)
    logger.info("  inserted %d new rows", n)
    return {"market": market, "symbol": symbol, "rows": len(rows),
            "inserted": n, "first": str(first), "last": str(last)}


def main() -> int:
    p = argparse.ArgumentParser(description="Taker flow archive backfill")
    p.add_argument("--market", choices=["spot", "futures", "both"],
                   default="both")
    p.add_argument("--symbols", default="ETHUSDT,BTCUSDT")
    p.add_argument("--probe", action="store_true",
                   help="Fetch and validate without writing.")
    args = p.parse_args()

    markets = ["futures", "spot"] if args.market == "both" else [args.market]
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = [run(m, s, args.probe) for m in markets for s in syms]
    print(f"\nResult: {results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
