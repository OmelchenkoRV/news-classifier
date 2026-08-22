"""
Binance public-archive backfill — recovers DELISTED symbol history.

WHY
---
`FINDINGS_survivorship.md` measured a ~75% inflation in the momentum result
from survivorship bias — but the test was itself incomplete, because the two
largest collapses of the cycle were missing:

    LUNCUSDT  data starts 2022-09-09  → the May-2022 Terra collapse ABSENT
    USTCUSDT  data starts 2023-03-10  → the UST de-peg ABSENT

The live REST API (`/api/v3/klines`) only serves currently-listed symbols, and
Binance relisted Terra post-collapse under NEW tickers. The original series are
gone from the API.

They are NOT gone from the public archive. `data.binance.vision` retains
monthly kline dumps for delisted pairs (Binance's own docs use ADABKRW — a
discontinued pair — as the worked example). This collector pulls from there.

    https://data.binance.vision/data/spot/monthly/klines/
        {SYMBOL}/{INTERVAL}/{SYMBOL}-{INTERVAL}-{YYYY-MM}.zip

THE TICKER-REUSE TRAP (read before adding symbols)
--------------------------------------------------
Binance REUSED `LUNAUSDT` for Terra 2.0 after the May-2022 collapse. Pre-June
2022 that ticker is the token that died; after, it is a DIFFERENT ASSET with a
different price path. Splicing them produces a fictional series that collapses
99.9% and then recovers — pure fantasy, and it would make survivorship look
BETTER than reality.

`config/universe.py` already documents this class of error for MATIC→POL. Here
it is handled with a hard per-symbol `end` cutoff in DEAD_SPECS. **Do not add a
symbol without checking whether its ticker was recycled.**

USAGE
-----
    python -m collectors.binance_archive --list          # show planned pulls
    python -m collectors.binance_archive --probe         # check availability
    python -m collectors.binance_archive                 # backfill all
    python -m collectors.binance_archive --symbol LUNAUSDT
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
logger = logging.getLogger("binance_archive")

BASE = "https://data.binance.vision/data/spot/monthly/klines"
INTERVAL = "1d"

# (symbol, start YYYY-MM, end YYYY-MM or None, note)
# `end` is a HARD CUTOFF for recycled tickers — see the trap above.
DEAD_SPECS = [
    # THE BIG ONE: original Terra LUNA. Ticker recycled for Terra 2.0 from
    # 2022-05, so we cut at 2022-05 and store under an ALIAS to avoid any
    # chance of colliding with the modern LUNAUSDT series.
    ("LUNAUSDT", "2020-11", "2022-05", "original Terra LUNA → ~0 (May 2022)"),
    ("USTUSDT",  "2021-01", "2022-05", "original TerraUSD → de-peg (May 2022)"),
    ("FTTUSDT",  "2020-11", "2022-12", "FTX token → collapse (Nov 2022)"),
    ("SRMUSDT",  "2020-11", "2022-12", "Serum, FTX-affiliated"),
    ("ANCUSDT",  "2022-01", "2022-12", "Anchor Protocol (Terra ecosystem)"),
    ("MIRUSDT",  "2021-04", "2022-12", "Mirror Protocol (Terra ecosystem)"),
    ("WAVESUSDT", "2020-11", "2024-06", "Waves ~99% drawdown"),
]

# Store recycled tickers under a distinct name so the dead series can never
# be confused with, or joined to, the live one.
SYMBOL_ALIAS = {
    "LUNAUSDT": "LUNAUSDT_DEAD",
    "USTUSDT": "USTUSDT_DEAD",
}


def month_range(start: str, end: str | None) -> list[str]:
    """['2021-01', '2021-02', ...] inclusive."""
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


def fetch_month(symbol: str, month: str) -> list | None:
    """Download+parse one monthly zip. None if absent (404 = no data)."""
    url = f"{BASE}/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{month}.zip"
    try:
        resp = requests.get(url, timeout=60)
    except Exception as e:
        logger.warning("  %s %s: request failed: %s", symbol, month, e)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        logger.warning("  %s %s: HTTP %s", symbol, month, resp.status_code)
        return None

    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        name = zf.namelist()[0]
        raw = zf.read(name).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("  %s %s: bad zip: %s", symbol, month, e)
        return None

    rows = []
    for rec in csv.reader(io.StringIO(raw)):
        if not rec or len(rec) < 5:
            continue
        # Newer archive files carry a header row; skip it.
        if not rec[0].strip().lstrip("-").isdigit():
            continue
        try:
            ts_raw = int(rec[0])
            # Binance switched open_time from ms to MICROseconds in newer
            # dumps. Detect by magnitude rather than assuming: ms epochs
            # for this era are ~1.6e12, microseconds ~1.6e15.
            ts = ts_raw / 1_000_000 if ts_raw > 1e14 else ts_raw / 1000
            rows.append({
                "open_time": datetime.fromtimestamp(ts, tz=timezone.utc),
                "open": float(rec[1]), "high": float(rec[2]),
                "low": float(rec[3]), "close": float(rec[4]),
                "volume": float(rec[5]) if len(rec) > 5 else 0.0,
            })
        except (ValueError, OSError, OverflowError):
            continue
    return rows


def store(symbol_out: str, rows: list) -> int:
    from config.database import get_connection, get_cursor
    from psycopg2.extras import execute_values
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        payload = [(symbol_out, r["open_time"], r["open"], r["high"],
                    r["low"], r["close"], r["volume"]) for r in rows]
        execute_values(cur, """
            INSERT INTO price_snapshots
                (symbol, timestamp, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT DO NOTHING
        """, payload, page_size=1000)
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def run_symbol(spec, probe: bool) -> dict:
    symbol, start, end, note = spec
    out_name = SYMBOL_ALIAS.get(symbol, symbol)
    months = month_range(start, end)
    logger.info("%s → %s  (%s … %s, %d months)  %s",
                symbol, out_name, start, end or "now", len(months), note)

    all_rows, found_months = [], 0
    for mo in months:
        rows = fetch_month(symbol, mo)
        if rows:
            found_months += 1
            all_rows.extend(rows)
            if probe:
                logger.info("  %s: %d rows", mo, len(rows))
    if not all_rows:
        logger.warning("  %s: NO DATA in archive for this range", symbol)
        return {"symbol": symbol, "rows": 0, "months": 0}

    first = min(r["open_time"] for r in all_rows).date()
    last = max(r["open_time"] for r in all_rows).date()
    logger.info("  %s: %d rows, %d/%d months, %s … %s",
                symbol, len(all_rows), found_months, len(months), first, last)

    if probe:
        return {"symbol": symbol, "rows": len(all_rows),
                "months": found_months, "first": str(first), "last": str(last)}

    n = store(out_name, all_rows)
    logger.info("  %s: %d new rows inserted as %s", symbol, n, out_name)
    return {"symbol": symbol, "stored_as": out_name, "rows": len(all_rows),
            "inserted": n, "first": str(first), "last": str(last)}


def main() -> int:
    p = argparse.ArgumentParser(description="Binance archive backfill")
    p.add_argument("--symbol", help="Only this symbol (must be in DEAD_SPECS).")
    p.add_argument("--probe", action="store_true",
                   help="Report availability without writing to the DB.")
    p.add_argument("--list", action="store_true",
                   help="Show planned pulls and exit.")
    args = p.parse_args()

    specs = DEAD_SPECS
    if args.symbol:
        specs = [s for s in specs if s[0] == args.symbol.upper()]
        if not specs:
            logger.error("%s not in DEAD_SPECS. Add it there first — and "
                         "check whether its ticker was recycled.", args.symbol)
            return 1

    if args.list:
        print(f"\n{'symbol':<12}{'stored as':<18}{'range':<20}note")
        for sym, st, en, note in specs:
            print(f"{sym:<12}{SYMBOL_ALIAS.get(sym, sym):<18}"
                  f"{st + ' … ' + (en or 'now'):<20}{note}")
        print("\nNOTE: `end` cutoffs exist because Binance RECYCLES tickers. "
              "LUNAUSDT after\n2022-05 is Terra 2.0 — a different asset. "
              "Splicing would fabricate a recovery.")
        return 0

    results = [run_symbol(s, args.probe) for s in specs]
    print(f"\nResult: {results}")
    if not args.probe:
        print("\nNext: add the recovered symbols to DEAD_CANDIDATES in "
              "scripts/backtest_survivorship.py\n(LUNAUSDT_DEAD, USTUSDT_DEAD) "
              "and re-run the survivorship test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
