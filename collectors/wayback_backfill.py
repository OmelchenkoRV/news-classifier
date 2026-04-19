"""
Wayback Machine Historical RSS Scraper
=========================================

The Internet Archive's Wayback Machine has cached snapshots of RSS feeds
going back years. This script pulls historical snapshots of our known
RSS feeds to backfill headlines beyond what APIs offer.

This is the key to getting 12+ months of training data for FREE.

How it works:
  1. Query Wayback CDX API for cached snapshots of each RSS feed URL
  2. Fetch each snapshot and parse it as RSS
  3. Extract headlines with timestamps
  4. Deduplicate and store in the headlines table

Rate limits: Wayback CDX API is free, but be gentle (1 req/sec).
Some feeds may have been cached infrequently.

Usage:
    python -m collectors.wayback_backfill --months 12
    python -m collectors.wayback_backfill --months 6 --feed "CoinDesk"
"""

import os
import sys
import time
import hashlib
import argparse
import logging
import json
from datetime import datetime, timezone, timedelta

import requests
import feedparser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

WAYBACK_CDX_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_RAW_URL = "https://web.archive.org/web/{timestamp}id_/{url}"

# High-value feeds to scrape historically (most likely to be cached)
PRIORITY_FEEDS = [
    ("CoinDesk",       "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph",  "https://cointelegraph.com/rss"),
    ("Decrypt",        "https://decrypt.co/feed"),
    ("Bitcoin Magazine","https://bitcoinmagazine.com/feed"),
    ("CryptoSlate",    "https://cryptoslate.com/feed/"),
    ("Bitcoinist",     "https://bitcoinist.com/feed/"),
    ("NewsBTC",        "https://www.newsbtc.com/feed/"),
    ("CryptoPotato",   "https://cryptopotato.com/feed/"),
    ("BeInCrypto",     "https://beincrypto.com/feed/"),
    ("U.Today",        "https://u.today/rss"),
]


def title_hash(title: str) -> str:
    normalized = title.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def extract_tickers(text: str) -> list:
    import re
    text_upper = text.upper()
    found = set()
    patterns = {
        "BTC": r"\bBTC\b|\bBITCOIN\b", "ETH": r"\bETH\b|\bETHEREUM\b",
        "SOL": r"\bSOL\b|\bSOLANA\b", "XRP": r"\bXRP\b|\bRIPPLE\b",
        "DOGE": r"\bDOGE\b|\bDOGECOIN\b", "ADA": r"\bADA\b|\bCARDANO\b",
        "TRX": r"\bTRX\b|\bTRON\b", "BNB": r"\bBNB\b",
    }
    for ticker, pattern in patterns.items():
        if re.search(pattern, text_upper):
            found.add(ticker)
    return sorted(found) if found else None


def get_wayback_snapshots(feed_url: str, from_date: str, to_date: str) -> list:
    """
    Query Wayback CDX API for cached snapshots of a feed URL.

    Args:
        feed_url: the RSS feed URL
        from_date: YYYYMMDD format
        to_date: YYYYMMDD format

    Returns:
        list of (timestamp, original_url) tuples
    """
    params = {
        "url": feed_url,
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype",
        "filter": "statuscode:200",
        "from": from_date,
        "to": to_date,
        "collapse": "timestamp:8",  # one snapshot per day max
    }

    resp = requests.get(WAYBACK_CDX_API, params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json()

    if not rows or len(rows) <= 1:
        return []

    # First row is header
    header = rows[0]
    snapshots = []
    for row in rows[1:]:
        record = dict(zip(header, row))
        # Only want RSS/XML content
        mime = record.get("mimetype", "")
        if "xml" in mime or "rss" in mime or "atom" in mime or "text" in mime:
            snapshots.append((record["timestamp"], record["original"]))

    return snapshots


def fetch_wayback_snapshot(timestamp: str, original_url: str) -> str:
    """Fetch the raw content of a Wayback Machine snapshot."""
    # id_ suffix tells Wayback to return raw content, not the framed version
    url = WAYBACK_RAW_URL.format(timestamp=timestamp, url=original_url)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_rss_content(content: str, source_name: str) -> list:
    """Parse RSS content and extract headline entries."""
    feed = feedparser.parse(content)
    entries = []

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        if not title or len(title) < 10:
            continue

        # Parse date
        pub_dt = None
        for field in ("published_parsed", "updated_parsed"):
            parsed = entry.get(field)
            if parsed:
                try:
                    pub_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                    break
                except Exception:
                    pass

        if not pub_dt:
            for field in ("published", "updated"):
                raw = entry.get(field)
                if raw:
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_dt = parsedate_to_datetime(raw).astimezone(timezone.utc)
                        break
                    except Exception:
                        pass

        if not pub_dt:
            continue

        entries.append({
            "title": title,
            "url": entry.get("link", ""),
            "published_at": pub_dt,
            "summary": (entry.get("summary") or "")[:500],
            "source_name": source_name.lower(),
        })

    return entries


def backfill_wayback(months: int = 12, feed_name: str = None):
    """
    Backfill historical headlines via Wayback Machine snapshots.
    """
    feeds = PRIORITY_FEEDS
    if feed_name:
        feeds = [(n, u) for n, u in PRIORITY_FEEDS if n.lower() == feed_name.lower()]
        if not feeds:
            logger.error(f"Feed '{feed_name}' not found in priority list")
            return

    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=months * 30)).strftime("%Y%m%d")
    to_date = now.strftime("%Y%m%d")

    conn = get_connection()
    cur = get_cursor(conn)

    cur.execute("""
        INSERT INTO collection_runs (source_type, metadata)
        VALUES ('wayback', %s::jsonb)
        RETURNING id
    """, (json.dumps({"months": months, "feeds": len(feeds)}),))
    run_id = cur.fetchone()["id"]
    conn.commit()

    total_new = 0
    total_fetched = 0
    total_snapshots = 0

    for feed_name, feed_url in feeds:
        logger.info(f"\n{feed_name}: searching Wayback for snapshots...")

        try:
            time.sleep(1)
            snapshots = get_wayback_snapshots(feed_url, from_date, to_date)
        except Exception as e:
            logger.warning(f"  CDX query failed: {e}")
            continue

        if not snapshots:
            logger.info(f"  No snapshots found")
            continue

        logger.info(f"  Found {len(snapshots)} snapshots")

        # Sample snapshots if too many (one per week is enough)
        if len(snapshots) > 60:
            step = max(1, len(snapshots) // 60)
            snapshots = snapshots[::step]
            logger.info(f"  Sampled to {len(snapshots)} (weekly)")

        feed_new = 0
        for snap_ts, snap_url in snapshots:
            total_snapshots += 1
            try:
                time.sleep(2)  # be gentle with Wayback
                content = fetch_wayback_snapshot(snap_ts, snap_url)
                entries = parse_rss_content(content, feed_name)
            except Exception as e:
                logger.debug(f"  Snapshot {snap_ts} failed: {e}")
                continue

            for entry in entries:
                total_fetched += 1
                h = title_hash(entry["title"])
                tickers = extract_tickers(entry["title"])

                try:
                    cur.execute("""
                        INSERT INTO headlines
                            (source_name, source_type, title, url, body_snippet,
                             published_at, tickers, title_hash)
                        VALUES (%s, 'wayback', %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (title_hash) DO NOTHING
                    """, (
                        entry["source_name"],
                        entry["title"],
                        entry["url"],
                        entry["summary"] if entry["summary"] else None,
                        entry["published_at"],
                        tickers,
                        h,
                    ))
                    if cur.rowcount > 0:
                        feed_new += 1
                        total_new += 1
                except Exception as e:
                    conn.rollback()

            conn.commit()

        logger.info(f"  {feed_name}: {feed_new} new headlines from {len(snapshots)} snapshots")

    # Update run
    cur.execute("""
        UPDATE collection_runs
        SET finished_at = NOW(),
            records_fetched = %s,
            records_new = %s,
            api_calls_used = %s,
            status = 'completed'
        WHERE id = %s
    """, (total_fetched, total_new, total_snapshots, run_id))
    conn.commit()
    conn.close()

    logger.info(f"\nWayback backfill complete:")
    logger.info(f"  Snapshots processed: {total_snapshots}")
    logger.info(f"  Headlines found: {total_fetched}")
    logger.info(f"  New (non-dup): {total_new}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill headlines from Wayback Machine")
    parser.add_argument("--months", type=int, default=12, help="Months back to search")
    parser.add_argument("--feed", type=str, help="Single feed name to backfill")
    args = parser.parse_args()

    backfill_wayback(months=args.months, feed_name=args.feed)
