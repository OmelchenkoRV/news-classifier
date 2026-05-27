"""
Schema migration: add detrending columns to trigger_outcomes.

Phase 6 detrending. Each outcome stores three measures of return:

  - return_pct           — raw return from fire to target (existing)
  - baseline_return_pct  — what the asset returned in the H-minute
                           window immediately BEFORE fire time
  - excess_return_pct    — return_pct - baseline_return_pct

The forecaster aggregates over either return_pct (raw, current default)
or excess_return_pct (detrended). Storing the baseline separately
preserves it for analysis — "during the peace-deal rally, what did
geopolitical-de-escalation triggers earn ABOVE the rally's drift?" is
a question excess_return_pct answers; "how strong was the underlying
drift those triggers fired into?" is what baseline_return_pct answers.

Applied to BOTH public and replay schemas. Idempotent — re-running
on an already-migrated DB is a no-op.

Usage:
    python -m config.migrate_detrending          # apply
    python -m config.migrate_detrending --check  # report status
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logger = logging.getLogger(__name__)


# Tables to migrate, in (schema, table) form. Replay schema is included
# so historical replay outcomes get the same columns and the backfill
# script can populate them.
TARGETS = [
    ("public", "trigger_outcomes"),
    ("replay", "trigger_outcomes"),
]

UP_TEMPLATE = """
ALTER TABLE {schema}.{table}
    ADD COLUMN IF NOT EXISTS excess_return_pct DOUBLE PRECISION;

ALTER TABLE {schema}.{table}
    ADD COLUMN IF NOT EXISTS baseline_return_pct DOUBLE PRECISION;
"""


def _schema_exists(cur, schema: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, schema: str, table: str, column: str) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
    """, (schema, table, column))
    return cur.fetchone() is not None


def check(conn=None) -> dict:
    """Report which tables have the new columns. Skips schemas that
    don't exist (e.g., replay not deployed)."""
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        cur = get_cursor(conn)
        result = {}
        for schema, table in TARGETS:
            if not _schema_exists(cur, schema):
                result[f"{schema}.{table}"] = "schema-missing"
                continue
            cols = {
                col: _column_exists(cur, schema, table, col)
                for col in ("excess_return_pct", "baseline_return_pct")
            }
            if all(cols.values()):
                result[f"{schema}.{table}"] = "migrated"
            elif any(cols.values()):
                result[f"{schema}.{table}"] = f"partial: {cols}"
            else:
                result[f"{schema}.{table}"] = "not-migrated"
        return result
    finally:
        if owned:
            conn.close()


def apply(conn=None) -> None:
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        cur = get_cursor(conn)
        for schema, table in TARGETS:
            if not _schema_exists(cur, schema):
                logger.info("Schema %s does not exist; skipping.", schema)
                continue
            sql = UP_TEMPLATE.format(schema=schema, table=table)
            logger.info("Applying detrending columns to %s.%s", schema, table)
            cur.execute(sql)
        if owned:
            conn.commit()
        logger.info("Migration applied successfully.")
    finally:
        if owned:
            conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="detrending columns migration")
    parser.add_argument("--check", action="store_true",
                        help="Report current state, do not modify.")
    args = parser.parse_args()

    if args.check:
        state = check()
        print("Detrending column state:")
        for table, status in state.items():
            mark = "✓" if status == "migrated" else "✗"
            print(f"  {mark} {table}: {status}")
        return 0 if all(s == "migrated" or s == "schema-missing"
                        for s in state.values()) else 1

    apply()
    state = check()
    bad = [(t, s) for t, s in state.items()
           if s != "migrated" and s != "schema-missing"]
    if bad:
        print("WARNING: post-apply state:")
        for t, s in bad:
            print(f"  ✗ {t}: {s}")
        return 1
    print("Migration complete.")
    for t, s in state.items():
        print(f"  ✓ {t}: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
