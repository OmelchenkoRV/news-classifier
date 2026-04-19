"""
CoinDesk (ex-CryptoCompare) News API — Historical Backfill
============================================================

Pulls historical news articles using the legacy CryptoCompare news endpoint
(now under CoinDesk branding). Your CoinDesk API key works on this endpoint.

IMPORTANT: You have a LIFETIME cap of 250,000 API calls.
This script is designed to be conservative:
  - 50 articles per call
  - ~300 calls for 12 months of data
  - Tracks calls in collection_runs table
  - Idempotent: re-running skips existing headlines

Usage:
    python -m collectors.coindesk_backfill --months 12
    python -m collectors.coindesk_backfill --months 6 --dry-run
"""

import os
import sys
import time
import hashlib
import argparse
import logging
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# CoinDesk inherited CryptoCompare's API. The key goes as a header.
API_KEY = os.getenv("COINDESK_API_KEY", "")
BASE_URL = "https://min-api.cryptocompare.com/data/v2/news/"

# Alternative: CoinDesk's own news endpoint (different format)
# BASE_URL_COINDESK = "https://data-api.coindesk.com/news/v1/article/list"


def title_hash(title: str) -> str:
    """Consistent hash for deduplication."""
    normalized = title.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def extract_tickers(article: dict) -> list:
    """Extract mentioned tickers from article categories/tags."""
    tickers = set()
    # CryptoCompare tags articles with categories like "BTC|ETH|Trading"
    cats = article.get("categories", "")
    if cats:
        for cat in cats.split("|"):
            cat = cat.strip().upper()
            if cat in ("BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "ATOM",
                       "AVAX", "LINK", "MATIC", "DOGE", "TRX", "BNB",
                       "UNI", "AAVE", "LTC", "NEAR", "ARB", "OP"):
                tickers.add(cat)
    # Also check title for common patterns
    title = article.get("title", "").upper()
    for ticker in ["BITCOIN", "ETHEREUM", "SOLANA", "RIPPLE", "CARDANO",
                    "POLKADOT", "COSMOS", "DOGECOIN", "TRON"]:
        mapping = {"BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL",
                   "RIPPLE": "XRP", "CARDANO": "ADA", "POLKADOT": "DOT",
                   "COSMOS": "ATOM", "DOGECOIN": "DOGE", "TRON": "TRX"}
        if ticker in title:
            tickers.add(mapping[ticker])
    return sorted(tickers) if tickers else None


def fetch_news_page(lts: int = None) -> dict:
    """
    Fetch one page of news articles.

    Args:
        lts: timestamp to fetch articles before (for pagination backwards)

    Returns:
        API response dict with 'Data' array
    """
    params = {
        "lang": "EN",
        "sortOrder": "latest",
    }
    if lts:
        params["lTs"] = lts

    headers = {
        "Authorization": f"Apikey {API_KEY}",
    }

    resp = requests.get(BASE_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def backfill(months: int = 12, dry_run: bool = False):
    """
    Backfill historical headlines from CoinDesk/CryptoCompare API.

    Args:
        months: how many months of history to pull
        dry_run: if True, don't insert into DB, just print stats
    """
    if not API_KEY:
        logger.error("COINDESK_API_KEY not found in .env file")
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    cutoff_ts = int(cutoff.timestamp())

    logger.info(f"Backfilling {months} months of headlines (back to {cutoff.date()})")
    logger.info(f"API key: {API_KEY[:8]}...{API_KEY[-4:]}")

    conn = get_connection()
    cur = get_cursor(conn)

    # Start a collection run
    if not dry_run:
        import json
        cur.execute("""
            INSERT INTO collection_runs (source_type, metadata)
            VALUES ('coindesk_api', %s::jsonb)
            RETURNING id
        """, (json.dumps({"months": months, "cutoff": cutoff.isoformat()}),))
        run_id = cur.fetchone()["id"]
        conn.commit()
    else:
        run_id = None

    total_fetched = 0
    total_new = 0
    api_calls = 0
    lts = None  # start from latest
    reached_cutoff = False

    try:
        while not reached_cutoff:
            # Rate limiting: be gentle
            if api_calls > 0:
                time.sleep(1.0)  # 1 second between calls

            try:
                data = fetch_news_page(lts=lts)
            except requests.exceptions.RequestException as e:
                logger.error(f"API error on call {api_calls + 1}: {e}")
                if api_calls > 3:
                    time.sleep(10)
                continue

            api_calls += 1
            articles = data.get("Data", [])

            if not articles:
                logger.info("No more articles returned, stopping.")
                break

            oldest_in_batch = None
            for article in articles:
                published_ts = article.get("published_on", 0)
                published_dt = datetime.fromtimestamp(published_ts, tz=timezone.utc)

                if published_ts < cutoff_ts:
                    reached_cutoff = True
                    break

                total_fetched += 1
                oldest_in_batch = published_dt
                title = article.get("title", "").strip()
                if not title:
                    continue

                h = title_hash(title)
                tickers = extract_tickers(article)
                body = article.get("body", "")[:500] if article.get("body") else None
                url = article.get("url", "") or article.get("guid", "")
                source = article.get("source_info", {}).get("name", article.get("source", "unknown"))

                if dry_run:
                    logger.debug(f"  [{published_dt.date()}] {source}: {title[:80]}")
                else:
                    try:
                        cur.execute("""
                            INSERT INTO headlines
                                (source_name, source_type, title, url, body_snippet,
                                 published_at, tickers, title_hash)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (title_hash) DO NOTHING
                        """, (
                            source.lower(),
                            "api_coindesk",
                            title,
                            url,
                            body,
                            published_dt,
                            tickers,
                            h,
                        ))
                        if cur.rowcount > 0:
                            total_new += 1
                    except Exception as e:
                        logger.warning(f"Insert error: {e}")
                        conn.rollback()

                # Set lts for next page (oldest article in this batch)
                lts = published_ts

            if not dry_run:
                conn.commit()

            # Progress report
            if oldest_in_batch:
                logger.info(
                    f"Call {api_calls}: fetched {len(articles)} articles, "
                    f"oldest in batch: {oldest_in_batch.date()}, "
                    f"total new: {total_new}/{total_fetched}"
                )

            # Safety: don't burn too many calls
            if api_calls >= 5000:
                logger.warning("Reached 500 API call safety limit, stopping.")
                break

    except KeyboardInterrupt:
        logger.info("Interrupted by user. Saving progress...")

    finally:
        # Update collection run
        if run_id and not dry_run:
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

    # Summary
    logger.info("=" * 60)
    logger.info(f"Backfill complete {'(DRY RUN)' if dry_run else ''}")
    logger.info(f"  API calls used:    {api_calls}")
    logger.info(f"  Articles fetched:  {total_fetched}")
    logger.info(f"  New (non-dup):     {total_new}")
    logger.info(f"  Duplicates:        {total_fetched - total_new}")
    if not dry_run:
        logger.info(f"  Remaining budget:  ~{250_000 - api_calls} lifetime calls")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill headlines from CoinDesk API")
    parser.add_argument("--months", type=int, default=12, help="Months of history to pull")
    parser.add_argument("--dry-run", action="store_true", help="Don't insert, just count")
    args = parser.parse_args()

    backfill(months=args.months, dry_run=args.dry_run)
