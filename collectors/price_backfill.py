"""
Binance Price Backfill
=======================

Pulls hourly OHLCV candles from Binance (free, no API key needed).
Used to cross-reference headlines with subsequent price moves.

Usage:
    python -m collectors.price_backfill --months 12
    python -m collectors.price_backfill --months 6 --symbol ETHUSDT
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime, timezone, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "1h"
LIMIT = 1000  # max per request


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list:
    """Fetch hourly klines from Binance."""
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": LIMIT,
    }
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def backfill_prices(months: int = 12, symbols: list = None):
    """
    Backfill hourly price data from Binance.

    Args:
        months: months of history
        symbols: list of trading pairs (default: BTCUSDT, ETHUSDT)
    """
    symbols = symbols or SYMBOLS
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    conn = get_connection()
    cur = get_cursor(conn)

    # Track run
    import json
    cur.execute("""
        INSERT INTO collection_runs (source_type, metadata)
        VALUES ('binance_prices', %s::jsonb)
        RETURNING id
    """, (json.dumps({"months": months, "symbols": symbols}),))
    run_id = cur.fetchone()["id"]
    conn.commit()

    total_inserted = 0

    for symbol in symbols:
        logger.info(f"Backfilling {symbol} hourly candles from {cutoff.date()}...")
        start_ms = cutoff_ms
        symbol_count = 0

        while start_ms < now_ms:
            try:
                klines = fetch_klines(symbol, start_ms, now_ms)
            except requests.exceptions.RequestException as e:
                logger.error(f"Binance error: {e}")
                time.sleep(5)
                continue

            if not klines:
                break

            for k in klines:
                # Kline format: [open_time, open, high, low, close, volume, ...]
                ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
                try:
                    cur.execute("""
                        INSERT INTO price_snapshots
                            (symbol, timestamp, open, high, low, close, volume)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (symbol, timestamp) DO NOTHING
                    """, (
                        symbol, ts,
                        float(k[1]), float(k[2]), float(k[3]),
                        float(k[4]), float(k[5]),
                    ))
                    if cur.rowcount > 0:
                        symbol_count += 1
                except Exception as e:
                    logger.warning(f"Insert error: {e}")
                    conn.rollback()

            conn.commit()

            # Move window forward
            last_ts = klines[-1][0]
            start_ms = last_ts + 1  # next millisecond after last candle

            # Rate limiting
            time.sleep(0.2)

        total_inserted += symbol_count
        logger.info(f"  {symbol}: {symbol_count} candles inserted")

    # Update run
    cur.execute("""
        UPDATE collection_runs
        SET finished_at = NOW(),
            records_new = %s,
            status = 'completed'
        WHERE id = %s
    """, (total_inserted, run_id))
    conn.commit()
    conn.close()

    logger.info(f"Price backfill complete: {total_inserted} total candles across {len(symbols)} symbols")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill hourly prices from Binance")
    parser.add_argument("--months", type=int, default=12, help="Months of history")
    parser.add_argument("--symbol", type=str, help="Single symbol to backfill")
    args = parser.parse_args()

    syms = [args.symbol] if args.symbol else None
    backfill_prices(months=args.months, symbols=syms)
