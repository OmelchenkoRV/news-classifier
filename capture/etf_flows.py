"""
ETF flow collector — populates the eth_etf_flows table.

CONTEXT
-------
The eth-capture schema defined `eth_etf_flows` but nothing ever wrote to it
(orphaned table — queries returned zero rows). This collector fills it.

DATA SOURCE — the page's embedded JSON (no API endpoint needed)
---------------------------------------------------------------
DefiLlama's /etfs page is a Next.js app that SHIPS ITS FULL DATA in a
`<script id="__NEXT_DATA__">` block in the page HTML. We don't need to find
a separate JSON API: we fetch the page and parse that embedded block. The
relevant path is:

    props.pageProps.flows = {
        "<unix_ts>": {"date": <unix>, "Bitcoin": <usd>, "Ethereum": <usd>,
                      "Solana": <usd>, "Hyperliquid": <usd>},
        ...
    }

This is the COMPLETE daily net-flow history in USD, from the ETF launch
(Jan 2024) to today — Bitcoin and Ethereum throughout, Solana from Jan 2026,
Hyperliquid from Feb 2026. Daily values; missing asset key for a day means
no flow recorded (treated as absent, not zero).

So unlike the forward-only positioning data, ETF flows DO have ~2 years of
history here. But note the honest caveat from the plan: that's still only
ONE crash (June 2026), ETFs being new — so flows are usable as a regime
descriptor, not a multi-cycle-validated signal. We capture the full series;
how it's used downstream is a separate decision.

Idempotent via UNIQUE(flow_date, ticker) + upsert: re-running refreshes
recent (revisable) days and no-ops on settled ones. We store one row per
(date, asset) using ticker = '<ASSET>-TOTAL' to match the table's ticker
column (per-issuer breakdown isn't in this JSON; only daily totals per asset).

USAGE
-----
    python -m capture.etf_flows                 # fetch page, parse, upsert
    python -m capture.etf_flows --dry-run        # parse + preview, no DB write
    python -m capture.etf_flows --show-raw        # print parsed sample
    python -m capture.etf_flows --url <pageurl>   # override page URL
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etf_flows")

PAGE_URL = os.getenv("ETF_PAGE_URL", "https://defillama.com/etfs")

# Match the Next.js data island. Non-greedy up to the closing script tag.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _extract_flows(html: str) -> dict:
    """Pull props.pageProps.flows out of the page's __NEXT_DATA__ JSON.
    Returns the flows dict {unix_str: {date, <asset>: usd, ...}} or {}."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        logger.error("could not find __NEXT_DATA__ block in page HTML")
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.error("failed to parse __NEXT_DATA__ JSON: %s", e)
        return {}
    try:
        return data["props"]["pageProps"]["flows"] or {}
    except (KeyError, TypeError):
        logger.error("flows not at props.pageProps.flows — page shape changed")
        return {}


# Assets we care about for the eth-capture book. Others (Solana, Hyperliquid)
# are captured too — harmless and possibly useful — but BTC/ETH are primary.
_ASSET_KEYS = ("Bitcoin", "Ethereum", "Solana", "Hyperliquid")


def _rows_from_flows(flows: dict) -> list:
    """Flatten {unix: {date, Bitcoin, Ethereum,...}} to
    (flow_date, ticker, net_flow_usd) tuples, one per (day, asset present)."""
    rows = []
    for _, rec in flows.items():
        ts = rec.get("date")
        if ts is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        for asset in _ASSET_KEYS:
            if asset in rec and rec[asset] is not None:
                rows.append((d, f"{asset.upper()}-TOTAL", float(rec[asset])))
    return rows


def collect(dry_run: bool, show_raw: bool, url: str) -> dict:
    try:
        resp = requests.get(url, timeout=30,
                            headers={"User-Agent": "eth-capture/1.0"})
        resp.raise_for_status()
    except Exception as e:
        logger.error("fetch failed for %s: %s", url, e)
        return {"parsed": 0, "inserted": 0, "error": str(e)}

    flows = _extract_flows(resp.text)
    rows = _rows_from_flows(flows)
    if not rows:
        logger.warning("parsed 0 flow rows — check the page structure.")
        return {"parsed": 0, "inserted": 0}

    dmin = min(r[0] for r in rows)
    dmax = max(r[0] for r in rows)
    logger.info("parsed %d flow rows across %d days (%s … %s)",
                len(rows), len({r[0] for r in rows}), dmin, dmax)

    if show_raw or dry_run:
        for r in rows[:12]:
            print("  ", r)
        logger.info("dry-run: %d rows parsed, not writing.", len(rows))
        return {"parsed": len(rows), "inserted": 0, "dry_run": True}

    conn = get_connection()
    inserted = updated = 0
    try:
        from psycopg2.extras import execute_values
        from psycopg2 import errors as pg_errors
        cur = get_cursor(conn)
        # Upsert; recent days get revised so refresh on conflict.
        for attempt in range(5):
            try:
                for d, ticker, flow in rows:
                    cur.execute("""
                        INSERT INTO eth_etf_flows
                            (captured_at, flow_date, ticker, net_flow_usd, aum_usd)
                        VALUES (NOW(), %s, %s, %s, NULL)
                        ON CONFLICT (flow_date, ticker) DO UPDATE
                          SET net_flow_usd = EXCLUDED.net_flow_usd,
                              captured_at = NOW()
                        RETURNING (xmax = 0) AS is_insert
                    """, (d, ticker, flow))
                    row = cur.fetchone()
                    if row and row["is_insert"]:
                        inserted += 1
                    else:
                        updated += 1
                conn.commit()
                break
            except (pg_errors.DeadlockDetected,
                    pg_errors.SerializationFailure) as e:
                conn.rollback()
                inserted = updated = 0
                if attempt == 4:
                    raise
                logger.warning("deadlock, retrying: %s", e)
                cur = get_cursor(conn)
    finally:
        conn.close()

    logger.info("ETF flows: inserted=%d updated=%d", inserted, updated)
    return {"parsed": len(rows), "inserted": inserted, "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF flow collector")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse but don't write to DB.")
    parser.add_argument("--show-raw", action="store_true",
                        help="Print parsed sample rows.")
    parser.add_argument("--url", default=PAGE_URL,
                        help=f"ETF page URL (default {PAGE_URL}).")
    args = parser.parse_args()
    stats = collect(args.dry_run, args.show_raw, args.url)
    print(f"\nResult: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
