"""
GDELT DOC 2.0 API — Historical Crypto News Scraper
=====================================================

GDELT monitors global online news and provides free API access to
a rolling 3-month window of articles. No API key needed.

This script queries for crypto-related headlines with timestamps,
which we can cross-reference with price data.

Limitations:
  - Rolling 3-month window only (can't go back further)
  - Returns titles + URLs, not full article text
  - Rate limit: be gentle (1 request per 5 seconds)
  - Results are global (not crypto-specific), so filtering needed

Usage:
    python -m collectors.gdelt_backfill
    python -m collectors.gdelt_backfill --query "ethereum ETF"
"""

import os
import sys
import time
import hashlib
import argparse
import logging
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# Search queries to cover the crypto space broadly
QUERIES = [
    "bitcoin OR BTC cryptocurrency",
    "ethereum OR ETH crypto",
    "crypto regulation SEC",
    "crypto exchange hack",
    "stablecoin USDT USDC",
    "crypto tariff sanctions",
    "bitcoin ETF",
    "DeFi exploit vulnerability",
    "crypto liquidation crash",
    "federal reserve crypto rates",
]


def title_hash(title: str) -> str:
    normalized = title.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def extract_tickers_from_title(title: str) -> list:
    """Simple ticker extraction from headline text."""
    import re
    title_upper = title.upper()
    found = set()
    patterns = {
        "BTC": r"\bBTC\b|\bBITCOIN\b",
        "ETH": r"\bETH\b|\bETHEREUM\b",
        "SOL": r"\bSOL\b|\bSOLANA\b",
        "XRP": r"\bXRP\b|\bRIPPLE\b",
        "DOGE": r"\bDOGE\b|\bDOGECOIN\b",
        "ADA": r"\bADA\b|\bCARDANO\b",
        "DOT": r"\bDOT\b|\bPOLKADOT\b",
        "ATOM": r"\bATOM\b|\bCOSMOS\b",
        "AVAX": r"\bAVAX\b|\bAVALANCHE\b",
        "LINK": r"\bLINK\b|\bCHAINLINK\b",
        "TRX": r"\bTRX\b|\bTRON\b",
        "BNB": r"\bBNB\b|\bBINANCE COIN\b",
    }
    for ticker, pattern in patterns.items():
        if re.search(pattern, title_upper):
            found.add(ticker)
    return sorted(found) if found else None


def fetch_gdelt_articles(query: str, start_date: str = None, end_date: str = None,
                         max_records: int = 250) -> list:
    """
    Fetch articles from GDELT DOC 2.0 API.

    Args:
        query: search terms
        start_date: YYYYMMDDHHMMSS format (optional)
        end_date: YYYYMMDDHHMMSS format (optional)
        max_records: max articles to return (max 250)

    Returns:
        list of article dicts with title, url, date, domain, tone
    """
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(min(max_records, 250)),
        "sort": "datedesc",
    }
    if start_date:
        params["startdatetime"] = start_date
    if end_date:
        params["enddatetime"] = end_date

    resp = requests.get(GDELT_DOC_API, params=params, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    articles = data.get("articles", [])
    return articles


def backfill_gdelt(queries: list = None, days_back: int = 85):
    """
    Backfill headlines from GDELT.

    Iterates over multiple search queries and time windows to get
    broad crypto news coverage for the last ~3 months.
    """
    queries = queries or QUERIES

    conn = get_connection()
    cur = get_cursor(conn)

    cur.execute("""
        INSERT INTO collection_runs (source_type, metadata)
        VALUES ('gdelt', %s::jsonb)
        RETURNING id
    """, (json.dumps({"queries": len(queries), "days_back": days_back}),))
    run_id = cur.fetchone()["id"]
    conn.commit()

    total_fetched = 0
    total_new = 0
    api_calls = 0
    now = datetime.now(timezone.utc)

    # Iterate in 7-day windows for each query
    for q_idx, query in enumerate(queries):
        logger.info(f"Query {q_idx+1}/{len(queries)}: {query}")

        window_start = now - timedelta(days=days_back)
        while window_start < now:
            window_end = min(window_start + timedelta(days=7), now)

            start_str = window_start.strftime("%Y%m%d%H%M%S")
            end_str = window_end.strftime("%Y%m%d%H%M%S")

            try:
                time.sleep(5)  # GDELT rate limit: be gentle
                articles = fetch_gdelt_articles(query, start_str, end_str)
                api_calls += 1
            except Exception as e:
                logger.warning(f"  GDELT error: {e}")
                window_start = window_end
                continue

            batch_new = 0
            for art in articles:
                title = (art.get("title") or "").strip()
                if not title or len(title) < 15:
                    continue

                # Parse GDELT date format (YYYYMMDDTHHMMSSZ or epoch)
                date_str = art.get("seendate", "")
                try:
                    if date_str:
                        pub_dt = datetime.strptime(date_str[:14], "%Y%m%d%H%M%S")
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    else:
                        continue
                except (ValueError, TypeError):
                    continue

                total_fetched += 1
                h = title_hash(title)
                url = art.get("url", "")
                domain = art.get("domain", "unknown")
                tickers = extract_tickers_from_title(title)

                # GDELT provides tone as a float
                tone = art.get("tone", 0)
                snippet = f"tone={tone:.1f}" if isinstance(tone, (int, float)) else None

                try:
                    cur.execute("""
                        INSERT INTO headlines
                            (source_name, source_type, title, url, body_snippet,
                             published_at, tickers, title_hash)
                        VALUES (%s, 'api_gdelt', %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (title_hash) DO NOTHING
                    """, (domain, title, url, snippet, pub_dt, tickers, h))
                    if cur.rowcount > 0:
                        batch_new += 1
                        total_new += 1
                except Exception as e:
                    conn.rollback()

            conn.commit()
            if batch_new > 0:
                logger.info(
                    f"  {window_start.strftime('%Y-%m-%d')} to {window_end.strftime('%Y-%m-%d')}: "
                    f"{batch_new} new / {len(articles)} fetched"
                )

            window_start = window_end

    # Update run
    cur.execute("""
        UPDATE collection_runs
        SET finished_at = NOW(),
            records_fetched = %s,
            records_new = %s,
            api_calls_used = %s,
            status = 'completed'
        WHERE id = %s
    """, (total_fetched, total_new, api_calls, run_id))
    conn.commit()
    conn.close()

    logger.info(f"\nGDELT backfill complete:")
    logger.info(f"  API calls: {api_calls}")
    logger.info(f"  Articles fetched: {total_fetched}")
    logger.info(f"  New (non-dup): {total_new}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill crypto headlines from GDELT")
    parser.add_argument("--days", type=int, default=85, help="Days back (max ~90)")
    parser.add_argument("--query", type=str, help="Single custom query")
    args = parser.parse_args()

    if args.query:
        backfill_gdelt(queries=[args.query], days_back=args.days)
    else:
        backfill_gdelt(days_back=args.days)
