"""
Backfill directional fields on trigger_mentions.

For trigger_mentions rows where direction IS NULL (typically because
Stage 2 was running degraded — e.g. spaCy not installed in the
container — at the time the mention was created), this script walks
those rows, runs the verb extractor on the associated headline title,
and writes direction / verb_category back to the mention row.

Idempotent: only operates on rows where direction IS NULL. Safe to
re-run; subsequent runs will pick up only newly-NULL rows (which
should be zero in steady state).

This script intentionally does NOT touch the `triggers` table. A
trigger's direction is set at creation time as the directional
split point — updating it post-hoc could create inconsistencies if
historical mentions disagree on direction. The known cost: if Stage 2
was off when a trigger was created, that trigger may have absorbed
both escalation and de-escalation mentions into a single signature.
Backfilling won't unmerge those. New triggers will route correctly.

Usage:
    python -m scripts.backfill_directions                 # do it
    python -m scripts.backfill_directions --dry-run       # report only
    python -m scripts.backfill_directions --batch 50      # commit every 50
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import get_connection, get_cursor
from inference.verb_extractor import VerbExtractor
from config.verb_taxonomy import UNKNOWN_CATEGORY

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill")


# Pull mentions that need backfilling, oldest first. Joining headlines
# to get the title — we need that to run extraction. The id ordering is
# stable so a re-run after a partial commit picks up where we left off.
SELECT_NULL_MENTIONS_SQL = """
    SELECT tm.id AS mention_id, tm.headline_id, h.title
    FROM trigger_mentions tm
    JOIN headlines h ON h.id = tm.headline_id
    WHERE tm.direction IS NULL
      AND h.title IS NOT NULL
    ORDER BY tm.id ASC
"""

UPDATE_MENTION_SQL = """
    UPDATE trigger_mentions
    SET direction = %s, verb_category = %s
    WHERE id = %s
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be backfilled without writing anything.",
    )
    p.add_argument(
        "--batch", type=int, default=100,
        help="Commit every N updates (default: 100). Smaller = safer "
             "against interruption, larger = faster.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Process at most this many rows. Default: all.",
    )
    args = p.parse_args()

    # Load the extractor once. If this fails, the whole backfill is
    # pointless — fail loud rather than degrading.
    try:
        extractor = VerbExtractor()
    except Exception as e:
        logger.error(
            "Failed to load VerbExtractor: %s\n"
            "Install: pip install spacy && python -m spacy download en_core_web_sm",
            e,
        )
        return 1

    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute(SELECT_NULL_MENTIONS_SQL)
        rows = cur.fetchall()
        if args.limit is not None:
            rows = rows[: args.limit]

        if not rows:
            logger.info("Nothing to backfill — no NULL-direction mentions found.")
            return 0

        logger.info(
            "Backfilling %d mention(s)%s",
            len(rows),
            " [DRY RUN]" if args.dry_run else "",
        )

        cat_counts: Counter[str] = Counter()
        dir_counts: Counter[str] = Counter()
        errors = 0
        updated = 0
        t0 = time.time()

        for i, row in enumerate(rows):
            try:
                result = extractor.extract(row["title"])
            except Exception as e:
                logger.warning(
                    "Extraction failed for mention_id=%s headline_id=%s: %s",
                    row["mention_id"], row["headline_id"], e,
                )
                errors += 1
                continue

            cat_counts[result.verb_category] += 1
            dir_counts[result.direction] += 1

            if not args.dry_run:
                cur.execute(
                    UPDATE_MENTION_SQL,
                    (result.direction, result.verb_category, row["mention_id"]),
                )
                updated += 1
                if updated % args.batch == 0:
                    conn.commit()
                    elapsed = time.time() - t0
                    rate = updated / elapsed if elapsed > 0 else 0
                    logger.info(
                        "  Progress: %d/%d (%.0f rows/sec)",
                        updated, len(rows), rate,
                    )

        if not args.dry_run:
            conn.commit()

        elapsed = time.time() - t0
        print()
        print("=== BACKFILL SUMMARY ===")
        print(f"  Total processed:  {len(rows)}")
        print(f"  Updated:          {updated}{'  [dry-run: not written]' if args.dry_run else ''}")
        print(f"  Errors:           {errors}")
        print(f"  Time:             {elapsed:.1f}s ({len(rows)/elapsed:.0f} rows/sec)")
        print()
        classified = sum(
            n for c, n in cat_counts.items()
            if c and c != UNKNOWN_CATEGORY
        )
        total = sum(cat_counts.values())
        if total:
            print(f"  Coverage:         {classified}/{total} = {classified/total:.1%}")
            print()
            print("  Direction breakdown:")
            for d in ("escalation", "de-escalation", "neutral",
                      "context-dependent", "unknown"):
                n = dir_counts.get(d, 0)
                print(f"    {d:<22} {n:>5}")
            print()
            print("  Top categories:")
            for cat, n in cat_counts.most_common(8):
                print(f"    {cat:<22} {n:>5}")
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
