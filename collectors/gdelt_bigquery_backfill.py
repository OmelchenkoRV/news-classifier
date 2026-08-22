"""
GDELT GKG historical backfill via BigQuery.

WHY THIS EXISTS
---------------
The DOC 2.0 API (collectors/gdelt_backfill.py) is rate-limited and capped
at 250 records per call — fine for sparse recent pulls, useless for dense
historical backfill of the 2022 crash windows. GDELT's full GKG 2.0 dataset
lives in BigQuery and is queryable with SQL back to Feb 2015, with 1 TB/month
of free query processing. That's the right tool for pulling 2022.

THE COST TRAP (read before running)
-----------------------------------
BigQuery bills by BYTES SCANNED, not rows returned. The GKG table is ~TBs.
A query that does NOT restrict the partition can scan the whole table and
burn your entire month's free 1 TB in a single query. The non-negotiable
rule, enforced in the SQL below:

    ALWAYS filter on _PARTITIONTIME to the exact date window.

The GKG table produces ~300–400k rows PER DAY, so we also filter to
crypto-relevant rows server-side (theme/keyword match in the SQL) so BigQuery
returns a small slice and we pay only for the partitions touched. A single
narrow window (a few weeks) filtered this way scans single-digit GB — well
inside free tier. Each query prints its bytes-scanned estimate so you can
watch the budget.

PREREQUISITES (one-time, on your machine)
-----------------------------------------
  1. A Google Cloud account + project (free tier; the 1 TB/month is free).
  2. Enable the BigQuery API for that project.
  3. Authenticate locally, simplest:  gcloud auth application-default login
     (or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key JSON).
  4. pip install google-cloud-bigquery db-dtypes
  5. Set GCP_PROJECT env var (or pass --project) to your project id.

HONEST LIMITATION
-----------------
GKG gives a reliable article URL and source name, but NOT always a clean
headline title. We derive a title from AllNames / the URL slug as a best
effort. These historical "headlines" are therefore lower-quality classifier
input than the live RSS feed — note this in any writeup (Step 6 of the plan).
For *systemic* events (FTX, LUNA) the signal is loud enough to survive
imperfect titles, but it is a real caveat.

USAGE
-----
    python -m collectors.gdelt_bigquery_backfill --start 2022-05-01 --end 2022-06-01
    python -m collectors.gdelt_bigquery_backfill --start 2022-11-01 --end 2022-12-01 --project my-gcp-proj
    python -m collectors.gdelt_bigquery_backfill --start 2024-07-01 --end 2024-08-01 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("gdelt_bq_backfill")


GKG_TABLE = "gdelt-bq.gdeltv2.gkg_partitioned"

# Server-side crypto filter. GKG V2Themes is a delimited list of GDELT
# theme codes; AllNames / Organizations carry entity strings. We match
# either a crypto keyword anywhere in the doc URL/names, OR systemic
# economic themes co-occurring with crypto terms. Kept broad enough to
# catch the systemic events (Terra/LUNA, Celsius, FTX) without pulling
# all 300-400k daily rows. Tuned conservatively — better to over-pull a
# few MB than miss the crash.
CRYPTO_REGEX = (
    r"(?i)(crypto|bitcoin|ethereum|btc|stablecoin|terra|luna|ust|"
    r"celsius|ftx|binance|coinbase|3ac|defi|tether|usdc|usdt)"
)


def _build_query(start: str, end: str) -> str:
    """Construct the partitioned, crypto-filtered GKG query. Both the
    _PARTITIONTIME filter (for billing) and the keyword filter (for
    relevance) are mandatory."""
    return f"""
        SELECT
            DATE,
            DocumentIdentifier AS url,
            SourceCommonName   AS source,
            V2Themes           AS themes,
            V2Persons          AS persons,
            V2Organizations    AS orgs,
            V2Tone             AS tone,
            AllNames           AS all_names
        FROM `{GKG_TABLE}`
        WHERE _PARTITIONTIME >= TIMESTAMP("{start}")
          AND _PARTITIONTIME <  TIMESTAMP("{end}")
          AND (
                REGEXP_CONTAINS(DocumentIdentifier, r'{CRYPTO_REGEX}')
             OR REGEXP_CONTAINS(IFNULL(AllNames, ''), r'{CRYPTO_REGEX}')
             OR REGEXP_CONTAINS(IFNULL(V2Organizations, ''), r'{CRYPTO_REGEX}')
          )
    """


def _derive_title(url: str, all_names: str) -> str:
    """Best-effort title from a GKG row. GKG has no clean title field, so:
    1) try the URL slug (often human-readable: /ftx-files-for-bankruptcy),
    2) fall back to the top AllNames entities.
    Returns '' if nothing usable — caller skips those rows."""
    # URL slug
    try:
        path = urlparse(url).path
        slug = path.rstrip("/").split("/")[-1]
        slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
        slug = slug.replace("-", " ").replace("_", " ").strip()
        # Reject slugs that are just ids/dates/numbers
        if len(slug) >= 15 and re.search(r"[a-zA-Z]", slug) and \
           len(re.findall(r"[a-zA-Z]+", slug)) >= 3:
            return slug[:300]
    except Exception:
        pass
    # Fallback: AllNames is "Name,offset;Name,offset;..." — take first few
    if all_names:
        names = [p.split(",")[0] for p in all_names.split(";") if p]
        names = [n for n in names if len(n) > 2][:6]
        if len(names) >= 2:
            return " ".join(names)[:300]
    return ""


def _title_hash(title: str) -> str:
    import hashlib
    return hashlib.sha256(title.lower().strip().encode()).hexdigest()


def run_backfill(start: str, end: str, project: str, dry_run: bool) -> dict:
    try:
        from google.cloud import bigquery
    except ImportError:
        logger.error("google-cloud-bigquery not installed. Run: "
                     "pip install google-cloud-bigquery db-dtypes")
        raise

    client = bigquery.Client(project=project)
    query = _build_query(start, end)

    # First do a DRY RUN to report bytes scanned BEFORE paying for it.
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry_job = client.query(query, job_config=dry_cfg)
    gb = dry_job.total_bytes_processed / 1e9
    logger.info("Estimated scan: %.2f GB (%.1f%% of monthly 1TB free tier)",
                gb, gb / 1000 * 100)

    if dry_run:
        logger.info("--dry-run: not executing. Query would scan %.2f GB.", gb)
        return {"scanned_gb": gb, "inserted": 0, "skipped": 0, "dry_run": True}

    if gb > 100:
        logger.warning("Query would scan %.0f GB — that's a large chunk of "
                       "your free tier. Narrow the window if unintended.", gb)

    # Execute
    rows = client.query(query).result()

    conn = get_connection()
    inserted = skipped = no_title = 0
    try:
        cur = get_cursor(conn)
        for r in rows:
            url = r["url"] or ""
            title = _derive_title(url, r["all_names"] or "")
            if not title:
                no_title += 1
                continue
            # GKG DATE is YYYYMMDDHHMMSS
            try:
                pub = datetime.strptime(str(r["DATE"])[:14], "%Y%m%d%H%M%S")
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
                    VALUES (%s, 'bq_gdelt_gkg', %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (title_hash) DO NOTHING
                """, (r["source"] or "gkg", title, url, snippet, pub,
                      None, _title_hash(title)))
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                conn.rollback()
                logger.debug("insert failed: %s", e)
        conn.commit()
    finally:
        conn.close()

    logger.info("Window %s→%s: inserted=%d skipped(dup)=%d no_title=%d "
                "(scanned %.2f GB)", start, end, inserted, skipped,
                no_title, gb)
    return {"scanned_gb": gb, "inserted": inserted,
            "skipped": skipped, "no_title": no_title}


def main() -> int:
    parser = argparse.ArgumentParser(description="GDELT GKG BigQuery backfill")
    parser.add_argument("--start", required=True, help="Window start YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="Window end YYYY-MM-DD (UTC, exclusive)")
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT"),
                        help="GCP project id (or set GCP_PROJECT env var)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report bytes-scanned estimate and exit without "
                             "querying or inserting. ALWAYS run this first on a "
                             "new window to check the cost.")
    args = parser.parse_args()

    if not args.project:
        parser.error("No GCP project. Pass --project or set GCP_PROJECT.")
    # Validate dates
    for d in (args.start, args.end):
        datetime.strptime(d, "%Y-%m-%d")

    stats = run_backfill(args.start, args.end, args.project, args.dry_run)
    print(f"\nResult: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
