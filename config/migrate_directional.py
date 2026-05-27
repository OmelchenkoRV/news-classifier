"""
Schema migration: add directional fields to triggers and trigger_mentions.

Adds, idempotently:
  triggers.direction              -- 'escalation' / 'de-escalation' / 'neutral'
                                  --   / 'context-dependent' / 'unknown' / NULL
  triggers.dominant_verb_category -- e.g. 'CLOSES', 'ATTACKS', or NULL
  trigger_mentions.direction      -- per-mention direction, NULL = unrecorded
  trigger_mentions.verb_category  -- per-mention verb category, NULL = unrecorded

Backfill behaviour:
  - Existing rows get NULL in the new columns. The trigger system treats
    NULL direction as "unknown / unclassified" and matches across any
    direction, which means existing triggers continue to absorb new
    headlines exactly as before until directional information starts
    flowing in.

Rollback: see DOWN_SQL at the bottom. Run manually if needed; this script
only goes forward.

Run with:
    python -m config.migrate_directional        # apply
    python -m config.migrate_directional --check # report state, no changes
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Allow running as `python -m config.migrate_directional` from project root,
# or directly as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logger = logging.getLogger(__name__)

UP_STATEMENTS = [
    # triggers.direction
    """
    ALTER TABLE triggers
    ADD COLUMN IF NOT EXISTS direction TEXT
    """,
    # triggers.dominant_verb_category
    """
    ALTER TABLE triggers
    ADD COLUMN IF NOT EXISTS dominant_verb_category TEXT
    """,
    # trigger_mentions.direction
    """
    ALTER TABLE trigger_mentions
    ADD COLUMN IF NOT EXISTS direction TEXT
    """,
    # trigger_mentions.verb_category
    """
    ALTER TABLE trigger_mentions
    ADD COLUMN IF NOT EXISTS verb_category TEXT
    """,
    # Index to support "find triggers with same anchors AND opposite direction"
    # lookups in process_headline. We don't index direction alone — too low
    # cardinality. Combined with category (already filtered) and state, the
    # candidate scan stays cheap.
    """
    CREATE INDEX IF NOT EXISTS idx_triggers_direction
    ON triggers (category, state, direction)
    WHERE state != 'ARCHIVED'
    """,
]

CHECK_QUERIES = {
    "triggers.direction":
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='triggers' AND column_name='direction'",
    "triggers.dominant_verb_category":
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='triggers' AND column_name='dominant_verb_category'",
    "trigger_mentions.direction":
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='trigger_mentions' AND column_name='direction'",
    "trigger_mentions.verb_category":
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='trigger_mentions' AND column_name='verb_category'",
    "idx_triggers_direction":
        "SELECT 1 FROM pg_indexes "
        "WHERE indexname='idx_triggers_direction'",
}

# For manual rollback. Not run by default — irreversible data loss.
DOWN_SQL = """
-- Rollback (DESTROYS direction data — review before running):
DROP INDEX IF EXISTS idx_triggers_direction;
ALTER TABLE trigger_mentions DROP COLUMN IF EXISTS verb_category;
ALTER TABLE trigger_mentions DROP COLUMN IF EXISTS direction;
ALTER TABLE triggers DROP COLUMN IF EXISTS dominant_verb_category;
ALTER TABLE triggers DROP COLUMN IF EXISTS direction;
"""


def check(conn=None) -> dict[str, bool]:
    """Return which migration artefacts already exist."""
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        cur = get_cursor(conn)
        result = {}
        for name, q in CHECK_QUERIES.items():
            cur.execute(q)
            result[name] = cur.fetchone() is not None
        return result
    finally:
        if owned:
            conn.close()


def apply(conn=None) -> None:
    """Apply migration. Idempotent — safe to re-run."""
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        cur = get_cursor(conn)
        for stmt in UP_STATEMENTS:
            logger.info("Executing: %s", stmt.strip().split("\n")[0])
            cur.execute(stmt)
        if owned:
            conn.commit()
        logger.info("Migration applied successfully.")
    finally:
        if owned:
            conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Directional schema migration")
    parser.add_argument("--check", action="store_true",
                        help="Report current schema state, do not modify.")
    args = parser.parse_args()

    if args.check:
        state = check()
        print("Current migration state:")
        for name, exists in state.items():
            mark = "✓" if exists else "✗"
            print(f"  {mark} {name}")
        return 0 if all(state.values()) else 1

    apply()
    state = check()
    if not all(state.values()):
        print("WARNING: post-apply check shows missing artefacts:")
        for name, exists in state.items():
            if not exists:
                print(f"  ✗ {name}")
        return 1
    print("Migration complete; all artefacts present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
