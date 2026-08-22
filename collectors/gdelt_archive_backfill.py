"""
GDELT GKG historical backfill via the raw file archive (no BigQuery, no
Google account, no payment card).

WHY THIS EXISTS
---------------
The DOC 2.0 API is rate-limited into uselessness for dense historical pulls,
and BigQuery requires a Google Cloud account with a payment card on file.
GDELT also publishes the raw GKG as freely-downloadable zipped CSVs — every
15 minutes since Feb 2015 — at deterministic URLs requiring no auth at all.
This collector downloads the files for a target window, filters to
crypto-relevant rows locally, and inserts into the same `headlines` table
the rest of the pipeline reads.

TRADE-OFF vs BigQuery
---------------------
BigQuery filtered server-side, so you pulled only crypto rows. Here we
download EVERY article's GKG row (all topics, ~300-400k rows/day globally)
and filter client-side. More download volume — a 30-day window is ~30×96 =
~2,880 files at a few MB each, several GB total — but completely free and
account-free. Slower, not harder.

URL SCHEME
----------
    http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.gkg.csv.zip
one file per 15-minute slot (000000, 001500, 003000, 004500, ...).
Some slots are genuinely missing (gaps in GDELT's own collection) — those
404 and we skip them, which is normal, not an error.

FORMAT GOTCHAS
--------------
  - The .csv files are TAB-delimited despite the extension. Parsing as
    comma shreds every row.
  - Column layout (GKG 2.1): col 0 = GKGRECORDID, col 1 = DATE
    (YYYYMMDDHHMMSS), col 4 = SourceCommonName, col 5 = DocumentIdentifier
    (URL), col 7 = V2Themes, ... col 11 = V2Persons, col 13 =
    V2Organizations, col 15 = V2Tone, col 23 = AllNames. We read by index
    defensively and tolerate short/garbled rows.
  - We reuse _derive_title / CRYPTO_REGEX / _title_hash from the BigQuery
    collector so title-derivation and the crypto filter are identical
    across both ingestion paths (one source of truth).

RESUMABLE & POLITE
------------------
Thousands of requests per window, so: a small delay between files, missing
files skipped, and a progress file (.gdelt_archive_progress) recording
completed slots so a re-run resumes instead of restarting.

USAGE
-----
    python -m collectors.gdelt_archive_backfill --start 2022-11-01 --end 2022-12-01
    python -m collectors.gdelt_archive_backfill --start 2022-05-01 --end 2022-06-01 --delay 0.3
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor
# Reuse the exact title-derivation and crypto filter from the BigQuery
# collector — one source of truth for both ingestion paths.
from collectors.gdelt_bigquery_backfill import (
    _derive_title, _title_hash, CRYPTO_REGEX,
)
import re

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("gdelt_archive_backfill")

# GDELT's V2Themes / AllNames fields for heavily-covered articles can be
# enormous — far past Python's default 131072-char csv field cap, which
# raises "_csv.Error: field larger than field limit". Raise the limit to
# the platform maximum so dense crash-day files (e.g. FTX, Nov 2022)
# parse. Guarded loop because sys.maxsize can overflow the C long on
# some platforms; step down until accepted.
_max = sys.maxsize
while True:
    try:
        csv.field_size_limit(_max)
        break
    except OverflowError:
        _max = int(_max // 10)


BASE_URL = "http://data.gdeltproject.org/gdeltv2"
PROGRESS_FILE = os.path.join(os.path.dirname(__file__), ".gdelt_archive_progress")
_CRYPTO = re.compile(CRYPTO_REGEX)

# GKG 2.1 column indices (0-based) for the fields we use.
COL_DATE = 1
COL_SOURCE = 4
COL_URL = 5
COL_THEMES = 7
COL_ORGS = 13
COL_TONE = 15
COL_ALLNAMES = 23
MIN_COLS = 24


def _slot_urls(start: datetime, end: datetime):
    """Yield (slot_dt, url) for every 15-minute slot in [start, end)."""
    t = start
    step = timedelta(minutes=15)
    while t < end:
        stamp = t.strftime("%Y%m%d%H%M%S")
        yield t, f"{BASE_URL}/{stamp}.gkg.csv.zip"
        t += step


def _load_progress() -> set:
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE) as f:
        return set(line.strip() for line in f if line.strip())


def _mark_done(stamp: str):
    with open(PROGRESS_FILE, "a") as f:
        f.write(stamp + "\n")


def _parse_gkg_zip(content: bytes):
    """Unzip and parse a GKG file. Yields dicts for crypto-relevant rows
    only. Tolerates short/garbled rows (GDELT data is occasionally
    malformed)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return
    for name in zf.namelist():
        raw = zf.read(name).decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(raw), delimiter="\t")
        for row in reader:
            if len(row) < MIN_COLS:
                continue
            url = row[COL_URL]
            all_names = row[COL_ALLNAMES]
            orgs = row[COL_ORGS]
            # Client-side crypto filter — same regex as the BQ path.
            blob = f"{url}\n{all_names}\n{orgs}"
            if not _CRYPTO.search(blob):
                continue
            yield {
                "date": row[COL_DATE],
                "url": url,
                "source": row[COL_SOURCE] or "gkg",
                "tone": row[COL_TONE],
                "all_names": all_names,
            }


def run_backfill(start: datetime, end: datetime, delay: float) -> dict:
    done = _load_progress()
    conn = get_connection()
    stats = {"files": 0, "missing": 0, "inserted": 0,
             "skipped": 0, "no_title": 0, "crypto_rows": 0}
    try:
        cur = get_cursor(conn)
        for slot_dt, url in _slot_urls(start, end):
            stamp = slot_dt.strftime("%Y%m%d%H%M%S")
            if stamp in done:
                continue
            try:
                resp = requests.get(url, timeout=60)
            except Exception as e:
                logger.warning("fetch error %s: %s", stamp, e)
                time.sleep(delay)
                continue

            if resp.status_code == 404:
                # Missing slot — normal, GDELT has gaps. Mark done so we
                # don't retry it on resume.
                stats["missing"] += 1
                _mark_done(stamp)
                continue
            if resp.status_code != 200:
                logger.warning("HTTP %d for %s", resp.status_code, stamp)
                time.sleep(delay)
                continue

            stats["files"] += 1
            batch_new = 0
            for r in _parse_gkg_zip(resp.content):
                stats["crypto_rows"] += 1
                title = _derive_title(r["url"], r["all_names"])
                if not title:
                    stats["no_title"] += 1
                    continue
                try:
                    pub = datetime.strptime(str(r["date"])[:14], "%Y%m%d%H%M%S")
                    pub = pub.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                tone_val = None
                if r["tone"]:
                    try:
                        tone_val = float(str(r["tone"]).split(",")[0])
                    except (ValueError, IndexError):
                        pass
                snippet = f"tone={tone_val:.1f}" if tone_val is not None else None
                try:
                    cur.execute("""
                        INSERT INTO headlines
                            (source_name, source_type, title, url, body_snippet,
                             published_at, tickers, title_hash)
                        VALUES (%s, 'archive_gdelt_gkg', %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (title_hash) DO NOTHING
                    """, (r["source"], title, r["url"], snippet, pub,
                          None, _title_hash(title)))
                    if cur.rowcount > 0:
                        stats["inserted"] += 1
                        batch_new += 1
                    else:
                        stats["skipped"] += 1
                except Exception:
                    conn.rollback()
            conn.commit()
            _mark_done(stamp)

            if stats["files"] % 96 == 0:   # ~once per day of data
                logger.info("%s — files=%d missing=%d inserted=%d crypto_rows=%d",
                            slot_dt.strftime("%Y-%m-%d %H:%M"),
                            stats["files"], stats["missing"],
                            stats["inserted"], stats["crypto_rows"])
            time.sleep(delay)
    finally:
        conn.close()

    logger.info("Backfill complete %s→%s: %s",
                start.date(), end.date(), stats)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="GDELT raw-archive GKG backfill")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC, exclusive)")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="Seconds between file requests (politeness). "
                             "Default 0.2.")
    parser.add_argument("--reset-progress", action="store_true",
                        help="Clear the resume checkpoint and start fresh.")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if start >= end:
        parser.error("--start must be before --end")

    if args.reset_progress and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        logger.info("Progress checkpoint cleared.")

    n_slots = int((end - start).total_seconds() // 900)
    logger.info("Window %s→%s ≈ %d fifteen-minute files (resumable).",
                start.date(), end.date(), n_slots)

    stats = run_backfill(start, end, args.delay)
    print(f"\nResult: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
