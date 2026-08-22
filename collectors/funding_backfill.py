"""
Funding-rate historical backfill — Binance USD-M perpetuals.

WHY THIS ONE IS BETTER-POWERED THAN THE OTHERS
----------------------------------------------
Every prior defensive-signal test was limited by data:
  - news-systemic  → degraded GKG titles (data-quality wall)
  - ETF flows      → 602 days, ONE cycle, no 2022 bear (NULL, see
                     docs/FINDINGS_aug2026_rally.md)
  - OI / long-short→ ~30-day exchange retention, forward-capture only

Funding is the exception: Binance serves the FULL history back to
contract launch (BTCUSDT perp: 2019-09), free, no API key. That covers
BOTH the 2022 bear AND the June-2026 crash — two bear markets instead of
one cycle. It is the best-powered free test available to this project.

Funding posts every 8h → ~3 rows/day/symbol → ~7.6k rows per symbol for
the full history. Tiny. No rate-limit concern (unlike the 1.39M GDELT
pull).

ENDPOINT
--------
GET https://fapi.binance.com/fapi/v1/fundingRate
    ?symbol=BTCUSDT&startTime=<ms>&endTime=<ms>&limit=1000

Returns: [{"symbol","fundingTime"(ms),"fundingRate"(str),"markPrice"}]
Paginates forward: advance startTime past the last fundingTime received.

MEASURED DESIGN NOTE (from the Aug-2026 analysis)
-------------------------------------------------
Binance funding SATURATES at the ±0.01%/8h cap (`0.0001`). During
2026-08-19→22 it pinned at exactly 0.0001 for three days straight — the
metric literally cannot express "more extreme" in the regimes where
discrimination matters most. So the downstream test must weight
funding MOMENTUM (rate of change) over funding LEVEL. We store the raw
rate; the analysis derives both.

USAGE
-----
    python -m collectors.funding_backfill --create-table
    python -m collectors.funding_backfill --symbols BTCUSDT,ETHUSDT \
        --start 2019-09-01
    python -m collectors.funding_backfill --dry-run --limit-pages 2
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("funding_backfill")

BASE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
PAGE_LIMIT = 1000          # API max
REQUEST_PAUSE = 0.25       # polite; well under Binance weight limits

DDL = """
CREATE TABLE IF NOT EXISTS funding_history (
    id           SERIAL PRIMARY KEY,
    symbol       TEXT        NOT NULL,
    funding_time TIMESTAMPTZ NOT NULL,
    funding_rate DOUBLE PRECISION NOT NULL,
    mark_price   DOUBLE PRECISION,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, funding_time)
);
CREATE INDEX IF NOT EXISTS idx_funding_symbol_time
    ON funding_history (symbol, funding_time);
"""


def _to_ms(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_page(symbol: str, start_ms: int, end_ms: int) -> list:
    """One page of funding history. Returns raw list (possibly empty)."""
    params = {"symbol": symbol, "startTime": start_ms,
              "endTime": end_ms, "limit": PAGE_LIMIT}
    resp = requests.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected response shape: {type(data)}")
    return data


def parse_rows(raw: list) -> list:
    """[(symbol, funding_time, funding_rate, mark_price), ...]"""
    rows = []
    for r in raw:
        try:
            rows.append((
                r["symbol"],
                datetime.fromtimestamp(r["fundingTime"] / 1000, tz=timezone.utc),
                float(r["fundingRate"]),
                float(r["markPrice"]) if r.get("markPrice") not in (None, "") else None,
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("skipping malformed funding record %r: %s", r, e)
    return rows


def backfill_symbol(symbol: str, start_ms: int, end_ms: int,
                    dry_run: bool, limit_pages: int | None) -> dict:
    """Page forward through the full range for one symbol."""
    all_rows, pages, cursor = [], 0, start_ms

    while cursor < end_ms:
        if limit_pages is not None and pages >= limit_pages:
            logger.info("  stopping early at --limit-pages=%d", limit_pages)
            break
        try:
            raw = fetch_page(symbol, cursor, end_ms)
        except Exception as e:
            logger.error("  %s page at %s failed: %s", symbol, cursor, e)
            break

        if not raw:
            break

        rows = parse_rows(raw)
        all_rows.extend(rows)
        pages += 1

        last_ms = raw[-1]["fundingTime"]
        if last_ms <= cursor:        # no forward progress — guard against loop
            break
        cursor = last_ms + 1

        logger.info("  %s: page %d, %d rows, through %s",
                    symbol, pages, len(rows),
                    datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).date())

        if len(raw) < PAGE_LIMIT:    # last page
            break
        time.sleep(REQUEST_PAUSE)

    if not all_rows:
        return {"symbol": symbol, "fetched": 0, "inserted": 0}

    span = (min(r[1] for r in all_rows).date(), max(r[1] for r in all_rows).date())
    logger.info("%s: %d rows fetched, %s … %s",
                symbol, len(all_rows), span[0], span[1])

    if dry_run:
        for r in all_rows[:5]:
            print("  ", r)
        return {"symbol": symbol, "fetched": len(all_rows),
                "inserted": 0, "dry_run": True, "span": span}

    conn = get_connection()
    inserted = 0
    try:
        from psycopg2.extras import execute_values
        cur = get_cursor(conn)
        # Self-healing: idempotent, costs nothing, removes the ordering
        # footgun of having to remember --create-table first.
        cur.execute(DDL)
        conn.commit()
        execute_values(cur, """
            INSERT INTO funding_history
                (symbol, funding_time, funding_rate, mark_price)
            VALUES %s
            ON CONFLICT (symbol, funding_time) DO NOTHING
        """, all_rows, page_size=1000)
        inserted = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    logger.info("%s: inserted %d new rows", symbol, inserted)
    return {"symbol": symbol, "fetched": len(all_rows),
            "inserted": inserted, "span": span}


def main() -> int:
    p = argparse.ArgumentParser(description="Binance funding-rate backfill")
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT",
                   help="Comma-separated perp symbols.")
    p.add_argument("--start", default="2019-09-01",
                   help="Start date YYYY-MM-DD (BTCUSDT perp launched 2019-09).")
    p.add_argument("--end", default=None, help="End date YYYY-MM-DD (default now).")
    p.add_argument("--create-table", action="store_true",
                   help="Create funding_history table then exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and parse, print sample, don't write.")
    p.add_argument("--limit-pages", type=int, default=None,
                   help="Stop after N pages per symbol (for testing).")
    args = p.parse_args()

    if args.create_table:
        conn = get_connection()
        try:
            cur = get_cursor(conn)
            cur.execute(DDL)
            conn.commit()
            logger.info("funding_history table ready.")
        finally:
            conn.close()
        return 0

    start_ms = _to_ms(args.start)
    end_ms = (_to_ms(args.end) if args.end
              else int(datetime.now(timezone.utc).timestamp() * 1000))

    stats = []
    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        logger.info("Backfilling %s from %s…", sym, args.start)
        stats.append(backfill_symbol(sym, start_ms, end_ms,
                                     args.dry_run, args.limit_pages))

    print(f"\nResult: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
