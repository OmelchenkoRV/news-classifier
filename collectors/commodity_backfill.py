"""
Commodity price backfill (yfinance → price_snapshots).

Pulls non-crypto, non-24/7 instruments and writes hourly OHLCV into
price_snapshots under our canonical symbol, the same table and schema
the crypto backfill uses. The outcomes worker resolves these outcomes
with the non-24/7 roll-forward logic in pipeline/outcomes.py — no
per-asset special-casing beyond the NON_24_7_ASSETS set.

Currently handles:
    WTI   ← CL=F   West Texas Intermediate crude (oil catalyst)
    GOLD  ← GC=F   Gold front-month future (safe-haven catalyst)

Both are routed from geopolitical/macro headlines in
config.asset_routing. Gold is included to test the safe-haven thesis:
fear bids gold; we let calibration reveal whether geopolitical news
actually moves gold's excess return, and in which direction relative
to oil — we do NOT assume an antiphase relationship.

Why yfinance:
  - Free, no API key.
  - Hourly granularity for ~730 days back (enough — we have no headline
    history older than that), daily for decades if ever needed.
  - CL=F / GC=F are the standard front-month proxies.

The catch — these markets are NOT 24/7:
  Futures have an overnight maintenance break plus weekend/holiday
  closures, so price_snapshots for these symbols has gaps that BTCUSDT
  never has. Outcome resolution handles this with a wider per-asset
  tolerance and roll-forward to the next session (see
  pipeline/outcomes.py NON_24_7_ASSETS). This collector's only job is
  to load whatever candles yfinance returns and never invent data for
  closed sessions.

Idempotent via ON CONFLICT (symbol, timestamp) DO NOTHING — safe to run
hourly on a schedule, same as the crypto backfill.

Usage:
    python -m collectors.commodity_backfill                  # all, 60d hourly
    python -m collectors.commodity_backfill --symbol WTI     # one only
    python -m collectors.commodity_backfill --days 730       # max hourly
    python -m collectors.commodity_backfill --interval 1d --days 3650
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("commodity_backfill")


# our price_snapshots symbol → yfinance ticker.
SYMBOL_TICKERS: dict[str, str] = {
    "WTI":  "CL=F",   # WTI front-month NYMEX crude future
    "GOLD": "GC=F",   # Gold front-month COMEX future
}


def fetch_symbol(our_symbol: str, ticker: str, interval: str, days: int):
    """Fetch OHLCV from yfinance for one instrument. Returns a list of
    (timestamp_utc, open, high, low, close, volume) tuples.

    yfinance is imported lazily so the live classifier doesn't carry it
    as a hard import-time dependency; a clear message is logged if it's
    missing."""
    try:
        import yfinance as yf
    except ImportError:
        logger.error(
            "yfinance not installed. It's in requirements.txt; "
            "install with: pip install yfinance"
        )
        raise

    # yfinance caps intraday history: 1h interval is limited to ~730d.
    if interval.endswith("h") or interval.endswith("m"):
        if days > 730:
            logger.warning(
                "Intraday interval %s is capped at 730d by yfinance; "
                "clamping from %d.", interval, days,
            )
            days = 730

    period = f"{days}d"
    logger.info("Fetching %s (%s) %s candles, period=%s",
                our_symbol, ticker, interval, period)

    df = yf.download(
        ticker, period=period, interval=interval,
        progress=False, auto_adjust=False,
    )
    if df is None or df.empty:
        logger.warning("yfinance returned no data for %s", ticker)
        return []

    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    import pandas as pd
    rows = []
    for ts, r in df.iterrows():
        ts = pd.Timestamp(ts)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        close = r.get("Close")
        if close is None or (isinstance(close, float) and close != close):
            continue
        rows.append((
            ts.to_pydatetime(),
            _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low")),
            float(close), _f(r.get("Volume")),
        ))
    logger.info("  parsed %d candles for %s", len(rows), our_symbol)
    return rows


def _f(v):
    """Coerce to float or None (NaN-safe)."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def store(our_symbol: str, rows) -> dict:
    """Insert candles for one symbol into price_snapshots. Idempotent."""
    if not rows:
        return {"inserted": 0, "skipped": 0}
    conn = get_connection()
    inserted = skipped = 0
    try:
        cur = get_cursor(conn)
        for ts, o, h, l, c, v in rows:
            cur.execute("""
                INSERT INTO price_snapshots
                    (symbol, timestamp, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, timestamp) DO NOTHING
                RETURNING id
            """, (our_symbol, ts, o, h, l, c, v))
            if cur.fetchone() is not None:
                inserted += 1
            else:
                skipped += 1
        conn.commit()
    finally:
        conn.close()
    return {"inserted": inserted, "skipped": skipped}


def backfill(symbols: list[str], interval: str, days: int) -> dict:
    """Backfill the named symbols. Each is independent — a failure on
    one is logged and the rest continue."""
    totals = {}
    for sym in symbols:
        ticker = SYMBOL_TICKERS.get(sym)
        if ticker is None:
            logger.warning("Unknown symbol %s; skipping.", sym)
            continue
        try:
            rows = fetch_symbol(sym, ticker, interval, days)
            stats = store(sym, rows)
            totals[sym] = stats
            logger.info("%s: inserted=%d skipped=%d",
                        sym, stats["inserted"], stats["skipped"])
        except Exception as e:
            logger.error("%s backfill failed: %s", sym, e)
            totals[sym] = {"error": str(e)}
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description="Commodity price backfill")
    parser.add_argument("--symbol", choices=sorted(SYMBOL_TICKERS), default=None,
                        help="Backfill only this symbol (default: all).")
    parser.add_argument("--interval", default="1h",
                        help="yfinance interval (1h, 1d, ...). Default 1h.")
    parser.add_argument("--days", type=int, default=60,
                        help="How many days back to fetch. Default 60.")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else list(SYMBOL_TICKERS)
    totals = backfill(symbols, args.interval, args.days)
    logger.info("Commodity backfill complete: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
