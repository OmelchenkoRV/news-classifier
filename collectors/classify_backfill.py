"""
Batch-classify unclassified headlines (Step 2 of the news-systemic plan).

Built for SCALE. The live pipeline classifies the RSS trickle one headline
at a time, which is fine for a few hundred a day. The GDELT historical
backfill produced hundreds of thousands of headlines, and classifying those
one-at-a-time on CPU would take most of a day. This script:

  - pulls unclassified headlines in pages from the DB,
  - runs them through the REAL batched classifier (inference.classifier
    .classify_batch, which does true tensor batching), and
  - writes results back, resumably.

Resumability is free via the classifications table's
UNIQUE(headline_id, model_version) + ON CONFLICT DO NOTHING: a headline that
is already classified is skipped, so re-running after an interruption picks
up where it left off. A 700k-row job WILL get interrupted; this makes that
a non-event.

USAGE
-----
    python -m scripts.classify_backfill
    python -m scripts.classify_backfill --batch 128 --source archive_gdelt_gkg
    python -m scripts.classify_backfill --limit 5000        # do a slice, e.g. to time it
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("classify_backfill")

MODEL_VERSION = "v2"
PAGE = 2000   # headlines fetched per DB page


def _fetch_page(cur, source: str | None, page_size: int) -> list:
    """Fetch a page of headlines lacking a v2 classification."""
    params: list = [MODEL_VERSION]
    src_clause = ""
    if source:
        src_clause = "AND h.source_type = %s"
        params.append(source)
    params.append(page_size)
    cur.execute(f"""
        SELECT h.id, h.title
        FROM headlines h
        LEFT JOIN classifications c
          ON c.headline_id = h.id AND c.model_version = %s
        WHERE c.id IS NULL
          {src_clause}
        ORDER BY length(h.title) ASC, h.id ASC
        LIMIT %s
    """, tuple(params))
    return cur.fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-classify headlines")
    parser.add_argument("--batch", type=int, default=64,
                        help="Model batch size for tensor batching (default 64).")
    parser.add_argument("--source", type=str, default=None,
                        help="Restrict to one source_type (e.g. "
                             "archive_gdelt_gkg). Default: all unclassified.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after roughly this many headlines "
                             "(for timing a slice). Default: all.")
    parser.add_argument("--quantize", action="store_true",
                        help="Apply int8 dynamic quantization for ~2-4x "
                             "faster CPU inference (tiny accuracy cost). "
                             "Recommended for large backfills.")
    parser.add_argument("--threads", type=int, default=None,
                        help="Override torch CPU thread count. On hybrid "
                             "Intel chips (P+E cores) fewer threads pinned "
                             "to performance cores (e.g. 6 or 8) can beat "
                             "the default. Worth A/B testing on a slice.")
    args = parser.parse_args()

    logger.info("Loading classifier model...")
    import torch
    # Use all physical cores for CPU inference. torch sometimes defaults
    # to a conservative thread count; setting it explicitly to the core
    # count is a free throughput win for a large batch job.
    try:
        n_cores = args.threads or os.cpu_count() or 4
        torch.set_num_threads(n_cores)
        logger.info("torch using %d threads", n_cores)
    except Exception:
        pass
    from inference.classifier import HeadlineClassifierInference
    clf = HeadlineClassifierInference(quantize=args.quantize)

    conn = get_connection()
    total_done = 0
    started = time.time()
    try:
        while True:
            cur = get_cursor(conn)
            page = _fetch_page(cur, args.source, PAGE)
            if not page:
                break

            ids = [r["id"] for r in page]
            titles = [r["title"] for r in page]
            results = clf.classify_batch(titles, batch_size=args.batch)

            # Write back in ONE bulk insert per page rather than 2000
            # individual round-trips. With per-row execute() the DB writes
            # serialized with (and against) the model loop; execute_values
            # collapses the page to a single statement. ON CONFLICT DO
            # NOTHING keeps it resumable.
            from psycopg2.extras import execute_values
            rows_to_insert = [
                (hid, res["impact_level"], res["impact_confidence"],
                 res["category"], res["category_confidence"],
                 res["is_causal"], res["news_impact_score"],
                 res["should_tighten_gates"], MODEL_VERSION)
                for hid, res in zip(ids, results)
            ]
            execute_values(cur, """
                INSERT INTO classifications
                    (headline_id, impact_level, impact_confidence,
                     category, category_confidence, is_causal,
                     news_impact_score, should_tighten_gates,
                     model_version)
                VALUES %s
                ON CONFLICT (headline_id, model_version) DO NOTHING
            """, rows_to_insert)
            conn.commit()

            total_done += len(page)
            rate = total_done / (time.time() - started)
            logger.info("classified %d (%.0f/s)", total_done, rate)

            if args.limit and total_done >= args.limit:
                logger.info("Reached --limit %d; stopping.", args.limit)
                break
    finally:
        conn.close()

    elapsed = time.time() - started
    logger.info("Done: %d headlines in %.0fs (%.0f/s)",
                total_done, elapsed, total_done / elapsed if elapsed else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
