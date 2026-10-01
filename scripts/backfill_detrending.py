"""
Backfill detrending columns on existing completed outcomes.

After deploying the schema migration that adds baseline_return_pct
and excess_return_pct, every completed outcome resolved by the worker
GOING FORWARD will have them populated. But existing rows (the ~2,300
replay outcomes from Phase 5c plus any forward-collected since then)
still have NULL values.

This script walks every completed outcome in both schemas, computes
the baseline and excess for each, and writes the values back. It uses
the same logic as the live worker — looks up the close price at
fire_time minus horizon, computes the prior-window return, subtracts
from return_pct.

Idempotent. Safe to re-run if the baseline rule changes (it would
overwrite the previous values). Re-running on already-populated rows
just rewrites them with the same values.

Usage:
    python -m scripts.backfill_detrending           # both schemas
    python -m scripts.backfill_detrending --schema replay
    python -m scripts.backfill_detrending --schema public --dry-run

The --dry-run flag walks the data and reports what WOULD be written
without committing. Useful as a sanity check before the first
production backfill.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import get_connection, get_cursor
from pipeline.outcomes import _lookup_close_price

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_detrending")


# How many rows to process before committing. Smaller = more recovery
# granularity, larger = less commit overhead. 500 ≈ a few seconds of
# work, fine for either schema's ~2-3k rows.
BATCH_SIZE = 500


def _set_search_path(conn, schema: str) -> None:
    """Switch the connection to read/write the named schema's tables."""
    if schema == "public":
        return
    from psycopg2 import sql
    cur = conn.cursor()
    cur.execute(sql.SQL("SET search_path TO {}, public").format(
        sql.Identifier(schema)
    ))
    cur.close()


def _schema_exists(conn, schema: str) -> bool:
    cur = get_cursor(conn)
    cur.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    )
    return cur.fetchone() is not None


def backfill_one_schema(schema: str, dry_run: bool) -> dict:
    """Walk completed outcomes in `schema`, compute and persist
    baseline + excess return where missing or out of date.

    Returns a stats dict for reporting."""
    conn = get_connection()
    if not _schema_exists(conn, schema):
        logger.info("Schema %s does not exist; skipping.", schema)
        conn.close()
        return {"schema": schema, "skipped": True}
    _set_search_path(conn, schema)

    stats = {
        "schema": schema,
        "scanned": 0,
        "updated": 0,
        "baseline_missing": 0,  # baseline_start_price not found
        "errors": 0,
    }

    try:
        # We process in id-order batches. Within each batch, recompute
        # baseline+excess and bulk-update. Filter to rows that are
        # completed and have a return_pct (the conditions that mean
        # "real outcome with raw return populated"). We re-write
        # excess/baseline regardless of their current value — this is
        # what makes the script re-runnable when the baseline rule
        # changes.
        cur = get_cursor(conn)
        cur.execute("""
            SELECT id, asset, horizon_minutes, trigger_fired_at,
                   return_pct, price_at_fire
            FROM trigger_outcomes
            WHERE status = 'completed'
              AND return_pct IS NOT NULL
            ORDER BY id ASC
        """)
        all_rows = cur.fetchall()
        total = len(all_rows)
        logger.info("[%s] Backfilling %d completed outcomes", schema, total)

        for start in range(0, total, BATCH_SIZE):
            batch = all_rows[start:start + BATCH_SIZE]
            for row in batch:
                stats["scanned"] += 1
                try:
                    baseline_window_start = (
                        row["trigger_fired_at"]
                        - timedelta(minutes=row["horizon_minutes"])
                    )
                    baseline_start_price = _lookup_close_price(
                        cur, row["asset"], baseline_window_start
                    )
                    if baseline_start_price is None or baseline_start_price <= 0:
                        # No price candle for the baseline window start.
                        # Leave columns NULL — same convention as live
                        # worker. Log for visibility but it's not an error.
                        stats["baseline_missing"] += 1
                        if not dry_run:
                            cur.execute("""
                                UPDATE trigger_outcomes
                                SET baseline_return_pct = NULL,
                                    excess_return_pct = NULL
                                WHERE id = %s
                            """, (row["id"],))
                        continue

                    fired_price = float(row["price_at_fire"])
                    baseline_return_pct = (
                        (fired_price - baseline_start_price)
                        / baseline_start_price * 100.0
                    )
                    excess_return_pct = (
                        float(row["return_pct"]) - baseline_return_pct
                    )

                    if not dry_run:
                        cur.execute("""
                            UPDATE trigger_outcomes
                            SET baseline_return_pct = %s,
                                excess_return_pct = %s
                            WHERE id = %s
                        """, (baseline_return_pct, excess_return_pct, row["id"]))
                    stats["updated"] += 1
                except Exception as e:
                    logger.warning(
                        "[%s] Failed on outcome id=%s: %s",
                        schema, row["id"], e,
                    )
                    stats["errors"] += 1

            if not dry_run:
                conn.commit()
            logger.info(
                "[%s] %d/%d processed (updated=%d, baseline_missing=%d, errors=%d)",
                schema, min(start + BATCH_SIZE, total), total,
                stats["updated"], stats["baseline_missing"], stats["errors"],
            )

        if dry_run:
            conn.rollback()
            logger.info("[%s] Dry run complete; no commits.", schema)
        else:
            conn.commit()
            logger.info("[%s] Backfill complete.", schema)

    finally:
        conn.close()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--schema", choices=["public", "replay", "both"], default="both",
        help="Schema to backfill (default: both).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Walk the data and report what would be written, but do "
             "not commit any changes.",
    )
    args = parser.parse_args()

    schemas = (["public", "replay"] if args.schema == "both"
               else [args.schema])
    total_stats = []
    for schema in schemas:
        stats = backfill_one_schema(schema, args.dry_run)
        total_stats.append(stats)

    print()
    print("=" * 60)
    print("DETRENDING BACKFILL SUMMARY" + (" (DRY RUN)" if args.dry_run else ""))
    print("=" * 60)
    for s in total_stats:
        if s.get("skipped"):
            print(f"  {s['schema']:<10} skipped (schema not present)")
            continue
        print(
            f"  {s['schema']:<10} scanned={s['scanned']} "
            f"updated={s['updated']} "
            f"baseline_missing={s['baseline_missing']} "
            f"errors={s['errors']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
