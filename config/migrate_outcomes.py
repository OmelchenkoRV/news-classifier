"""
Schema migration: add trigger_outcomes table.

The trigger_outcomes table records, for each trigger that fires, the asset
return at fixed horizons (1h, 4h, 24h for BTC and ETH). It is the input
to the forecaster: aggregating outcomes by (trigger signature root,
direction, asset, horizon) yields the topic-conditional priors that the
plan describes.

Schema design notes:

  - Narrow rows: one row per (trigger, asset, horizon) instead of a wide
    table with separate BTC/ETH columns. Adding new assets later requires
    no schema change, just more rows.

  - `trigger_fired_at` is stored explicitly on the row rather than joined
    from `triggers.first_seen_at`. This is because replayed historical
    triggers (Phase 5c) will have first_seen_at = "now" (when the replay
    ran) but the *event* fired at the headline's published_at. The worker
    that computes outcomes needs the event time, not the row time.

  - `target_at` = trigger_fired_at + horizon. Computed once at scheduling
    time so the worker doesn't have to recompute on every poll.

  - `return_pct` is NULL until the worker resolves the outcome. Status
    column carries the lifecycle: 'pending' → 'completed' or
    'unavailable' (e.g. price data missing for that timestamp).

  - Indexes support the worker's "find pending outcomes whose target_at
    has passed" query and the forecaster's "for this signature root and
    direction, give me all completed outcomes" query.

Idempotent: ADD COLUMN IF NOT EXISTS pattern, CREATE TABLE IF NOT EXISTS,
CREATE INDEX IF NOT EXISTS. Safe to re-run.

Run with:
    python -m config.migrate_outcomes        # apply
    python -m config.migrate_outcomes --check # report state, no changes
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logger = logging.getLogger(__name__)


UP_STATEMENTS = [
    # Main outcomes table.
    """
    CREATE TABLE IF NOT EXISTS trigger_outcomes (
        id                  SERIAL PRIMARY KEY,
        trigger_id          INTEGER NOT NULL REFERENCES triggers(id) ON DELETE CASCADE,
        trigger_fired_at    TIMESTAMPTZ NOT NULL,
        asset               TEXT NOT NULL,         -- 'BTCUSDT' / 'ETHUSDT'
        horizon_minutes     INTEGER NOT NULL,      -- 60 / 240 / 1440
        target_at           TIMESTAMPTZ NOT NULL,  -- trigger_fired_at + horizon
        status              TEXT NOT NULL DEFAULT 'pending',
                                                   -- 'pending' / 'completed' / 'unavailable'
        return_pct          DOUBLE PRECISION,      -- NULL until resolved
        price_at_fire       DOUBLE PRECISION,      -- close price at fire time
        price_at_target     DOUBLE PRECISION,      -- close price at target time
        resolved_at         TIMESTAMPTZ,           -- when the worker computed it
        attempts            INTEGER NOT NULL DEFAULT 0,
                                                   -- count of resolution attempts
        last_attempt_at     TIMESTAMPTZ,
        last_error          TEXT,                  -- diagnostic for failed attempts
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (trigger_id, asset, horizon_minutes)
    )
    """,
    # The worker's primary query: "find outcomes whose target time has
    # passed and which are still pending". Partial index keeps it small.
    """
    CREATE INDEX IF NOT EXISTS idx_outcomes_pending_target
    ON trigger_outcomes (target_at)
    WHERE status = 'pending'
    """,
    # Forecaster's primary query: "give me all completed outcomes for
    # triggers matching some criterion". Trigger filter happens via JOIN;
    # this index speeds the outcome-side filter.
    """
    CREATE INDEX IF NOT EXISTS idx_outcomes_completed
    ON trigger_outcomes (asset, horizon_minutes, trigger_id)
    WHERE status = 'completed'
    """,
]

CHECK_QUERIES = {
    "trigger_outcomes table":
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name='trigger_outcomes'",
    "trigger_outcomes.trigger_fired_at":
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='trigger_outcomes' AND column_name='trigger_fired_at'",
    "trigger_outcomes.return_pct":
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='trigger_outcomes' AND column_name='return_pct'",
    "idx_outcomes_pending_target":
        "SELECT 1 FROM pg_indexes "
        "WHERE indexname='idx_outcomes_pending_target'",
    "idx_outcomes_completed":
        "SELECT 1 FROM pg_indexes "
        "WHERE indexname='idx_outcomes_completed'",
}

# For manual rollback. Not run by default — irreversible data loss.
DOWN_SQL = """
-- Rollback (DESTROYS outcome data — review before running):
DROP INDEX IF EXISTS idx_outcomes_completed;
DROP INDEX IF EXISTS idx_outcomes_pending_target;
DROP TABLE IF EXISTS trigger_outcomes;
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
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="trigger_outcomes schema migration")
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
