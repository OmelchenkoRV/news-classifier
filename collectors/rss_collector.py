"""
Live RSS Feed Collector
========================

Polls all active RSS feeds from the rss_sources table, parses new
headlines, deduplicates, and stores them. Designed to run as a
daemon on the VPS or as a scheduled task.

Features:
  - Multi-source with per-source error tracking
  - Auto-disables feeds after 10 consecutive errors
  - Deduplicates by title hash
  - Extracts tickers from title text
  - Graceful handling of malformed feeds

Usage:
    python -m collectors.rss_collector              # single run
    python -m collectors.rss_collector --daemon     # continuous (every 5 min)
    python -m collectors.rss_collector --test-feeds # verify which feeds work
"""

import os
import sys
import time
import hashlib
import argparse
import logging
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Ticker detection patterns
TICKER_MAP = {
    r'\bbitcoin\b': 'BTC', r'\bbtc\b': 'BTC',
    r'\bethereum\b': 'ETH', r'\beth\b': 'ETH',
    r'\bsolana\b': 'SOL', r'\bsol\b': 'SOL',
    r'\bripple\b': 'XRP', r'\bxrp\b': 'XRP',
    r'\bcardano\b': 'ADA', r'\bada\b': 'ADA',
    r'\bpolkadot\b': 'DOT', r'\bdot\b': 'DOT',
    r'\bcosmos\b': 'ATOM', r'\batom\b': 'ATOM',
    r'\bdogecoin\b': 'DOGE', r'\bdoge\b': 'DOGE',
    r'\btron\b': 'TRX', r'\btrx\b': 'TRX',
    r'\bavalanche\b': 'AVAX', r'\bavax\b': 'AVAX',
    r'\bchainlink\b': 'LINK', r'\blink\b': 'LINK',
    r'\buniswap\b': 'UNI', r'\buni\b': 'UNI',
    r'\baave\b': 'AAVE',
    r'\blitecoin\b': 'LTC', r'\bltc\b': 'LTC',
    r'\bbnb\b': 'BNB', r'\bbinance coin\b': 'BNB',
    r'\bstablecoin\b': 'STABLE', r'\busdt\b': 'USDT', r'\busdc\b': 'USDC',
    r'\btether\b': 'USDT',
}


def title_hash(title: str) -> str:
    normalized = title.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def extract_tickers(text: str) -> list:
    text_lower = text.lower()
    found = set()
    for pattern, ticker in TICKER_MAP.items():
        if re.search(pattern, text_lower):
            found.add(ticker)
    # Don't return generic markers
    found.discard("STABLE")
    return sorted(found) if found else None


def parse_pub_date(entry) -> datetime:
    """Extract publication date from RSS entry."""
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for field in ("published", "updated"):
        raw = entry.get(field)
        if raw:
            try:
                return parsedate_to_datetime(raw).astimezone(timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def fetch_feed(url: str, timeout: int = 15) -> list:
    """Fetch and parse an RSS feed, return list of entries."""
    feed = feedparser.parse(url, request_headers={"User-Agent": "NewsCryptoClassifier/1.0"})
    if feed.bozo and not feed.entries:
        raise ValueError(f"Feed parse error: {feed.bozo_exception}")
    return feed.entries


def collect_once():
    """Single collection run across all active feeds."""
    conn = get_connection()
    cur = get_cursor(conn)

    # Get active feeds
    cur.execute("""
        SELECT id, name, url, tier
        FROM rss_sources
        WHERE is_active = TRUE
        ORDER BY tier, name
    """)
    feeds = cur.fetchall()

    if not feeds:
        logger.warning("No active RSS feeds in database. Run: python -m collectors.rss_sources")
        conn.close()
        return

    # Start collection run
    import json as _json
    cur.execute("""
        INSERT INTO collection_runs (source_type, metadata)
        VALUES ('rss', %s::jsonb)
        RETURNING id
    """, (_json.dumps({"feeds": len(feeds)}),))
    run_id = cur.fetchone()["id"]
    conn.commit()

    total_new = 0
    total_fetched = 0
    feeds_ok = 0
    feeds_failed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)  # ignore articles older than 7 days

    for feed in feeds:
        feed_id = feed["id"]
        feed_name = feed["name"]
        feed_url = feed["url"]

        try:
            entries = fetch_feed(feed_url)
            feeds_ok += 1

            # Reset error count on success
            cur.execute("""
                UPDATE rss_sources
                SET last_fetched_at = NOW(), error_count = 0, last_error = NULL
                WHERE id = %s
            """, (feed_id,))

            feed_new = 0
            for entry in entries:
                title = (entry.get("title") or "").strip()
                if not title or len(title) < 10:
                    continue

                pub_date = parse_pub_date(entry)
                if pub_date < cutoff:
                    continue

                total_fetched += 1
                h = title_hash(title)
                link = entry.get("link", "")
                summary = (entry.get("summary") or "")[:500]
                tickers = extract_tickers(title + " " + summary)

                try:
                    cur.execute("""
                        INSERT INTO headlines
                            (source_name, source_type, title, url, body_snippet,
                             published_at, tickers, title_hash)
                        VALUES (%s, 'rss', %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (title_hash) DO NOTHING
                    """, (
                        feed_name.lower(),
                        title,
                        link,
                        summary if summary else None,
                        pub_date,
                        tickers,
                        h,
                    ))
                    if cur.rowcount > 0:
                        feed_new += 1
                        total_new += 1
                except Exception as e:
                    conn.rollback()

            conn.commit()
            if feed_new > 0:
                logger.info(f"  {feed_name}: {feed_new} new headlines")

        except Exception as e:
            feeds_failed += 1
            error_msg = str(e)[:200]

            cur.execute("""
                UPDATE rss_sources
                SET error_count = error_count + 1,
                    last_error = %s,
                    is_active = CASE WHEN error_count >= 9 THEN FALSE ELSE TRUE END
                WHERE id = %s
            """, (error_msg, feed_id))
            conn.commit()

            # Only log if not a known-dead feed
            cur.execute("SELECT error_count FROM rss_sources WHERE id = %s", (feed_id,))
            err_count = cur.fetchone()["error_count"]
            if err_count <= 3:
                logger.warning(f"  {feed_name}: FAILED ({error_msg[:80]})")
            elif err_count == 10:
                logger.error(f"  {feed_name}: DISABLED after 10 consecutive errors")

    # Update collection run
    cur.execute("""
        UPDATE collection_runs
        SET finished_at = NOW(),
            records_fetched = %s,
            records_new = %s,
            status = 'completed',
            metadata = metadata || %s::jsonb
        WHERE id = %s
    """, (total_fetched, total_new,
          _json.dumps({"feeds_ok": feeds_ok, "feeds_failed": feeds_failed}),
          run_id))
    conn.commit()
    conn.close()

    logger.info(
        f"RSS collection done: {total_new} new / {total_fetched} total "
        f"from {feeds_ok} feeds ({feeds_failed} failed)"
    )


def test_feeds():
    """Test which feeds are reachable and return valid data."""
    conn = get_connection()
    cur = get_cursor(conn)

    cur.execute("SELECT name, url, tier FROM rss_sources WHERE is_active = TRUE ORDER BY tier, name")
    feeds = cur.fetchall()
    conn.close()

    print(f"\nTesting {len(feeds)} RSS feeds...\n")
    ok = 0
    broken = 0
    for f in feeds:
        try:
            entries = fetch_feed(f["url"], timeout=10)
            entry_count = len(entries)
            latest = entries[0].get("title", "?")[:60] if entries else "empty"
            print(f"  OK  T{f['tier']} {f['name']:25s}  {entry_count:>3d} entries  |  {latest}")
            ok += 1
        except Exception as e:
            print(f"  ERR T{f['tier']} {f['name']:25s}  {str(e)[:60]}")
            broken += 1

    print(f"\n{ok} working / {broken} broken out of {len(feeds)} feeds")


def daemon(interval_seconds: int = 300):
    """Run collection continuously."""
    logger.info(f"Starting RSS daemon, polling every {interval_seconds}s...")
    while True:
        try:
            collect_once()
        except Exception as e:
            logger.error(f"Collection run failed: {e}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RSS feed collector")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--test-feeds", action="store_true", help="Test which feeds work")
    parser.add_argument("--interval", type=int, default=300, help="Polling interval in seconds")
    args = parser.parse_args()

    if args.test_feeds:
        test_feeds()
    elif args.daemon:
        daemon(interval_seconds=args.interval)
    else:
        collect_once()
