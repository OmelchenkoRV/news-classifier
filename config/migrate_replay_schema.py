"""
Schema migration: create the `replay` schema with parallel tables.

The replay schema mirrors the trigger-related tables from `public`:
  replay.triggers
  replay.trigger_mentions
  replay.trigger_outcomes

Plus a small bookkeeping table:
  replay.replay_progress

Read-only tables (headlines, classifications, price_snapshots) are NOT
duplicated — they stay in `public` and are accessed via search_path.

The replay script runs with `SET search_path = replay, public`. SQL like
`INSERT INTO triggers ...` resolves to `replay.triggers` (the writable
copy); `SELECT FROM headlines ...` resolves to `public.headlines` (the
shared read-only copy). This lets us reuse pipeline.triggers's
process_headline unchanged — no code path needs to know about the
replay schema explicitly.

The `replay_progress` table tracks how far the replay walker has
advanced through the headline stream so a crashed run can resume
without re-processing everything.

Usage:
    python -m config.migrate_replay_schema        # apply
    python -m config.migrate_replay_schema --check # check status
    python -m config.migrate_replay_schema --drop  # destroy ALL replay data

Idempotent on apply (CREATE ... IF NOT EXISTS). The --drop flag is the
escape hatch when you want to start over with a fresh replay.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logger = logging.getLogger(__name__)


# Parallel structure to public.triggers / public.trigger_mentions /
# public.trigger_outcomes. We could DRY this by introspecting the
# public schema and copying definitions — but that's clever, brittle,
# and obscures what the replay tables actually look like. Explicit
# wins; if the public schema changes, this migration needs updating
# too, and that's a feature.
UP_STATEMENTS = [
    "CREATE SCHEMA IF NOT EXISTS replay",

    # Mirrors public.triggers.
    """
    CREATE TABLE IF NOT EXISTS replay.triggers (
        id                      SERIAL PRIMARY KEY,
        signature               TEXT NOT NULL UNIQUE,
        display_name            TEXT NOT NULL,
        category                TEXT NOT NULL,
        first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        state                   TEXT NOT NULL DEFAULT 'ACTIVE',
        impact_score            DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        mention_count           INTEGER NOT NULL DEFAULT 1,
        keywords                TEXT[] NOT NULL,
        entities                TEXT[],
        actor_pairs             TEXT[],
        first_headline_id       INTEGER,
        first_title             TEXT,
        direction               TEXT,
        dominant_verb_category  TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_replay_triggers_signature
    ON replay.triggers (signature)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_replay_triggers_category_state
    ON replay.triggers (category, state)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_replay_triggers_last_seen_at
    ON replay.triggers (last_seen_at DESC)
    """,

    # Mirrors public.trigger_mentions. Note that headline_id refers to
    # public.headlines (the FK constraint references the public schema
    # explicitly so the search_path doesn't accidentally make this a
    # circular self-reference).
    """
    CREATE TABLE IF NOT EXISTS replay.trigger_mentions (
        id                  SERIAL PRIMARY KEY,
        trigger_id          INTEGER NOT NULL REFERENCES replay.triggers(id) ON DELETE CASCADE,
        headline_id         INTEGER NOT NULL REFERENCES public.headlines(id) ON DELETE CASCADE,
        observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        caused_creation     BOOLEAN NOT NULL DEFAULT FALSE,
        caused_refresh      BOOLEAN NOT NULL DEFAULT FALSE,
        similarity_score    DOUBLE PRECISION,
        direction           TEXT,
        verb_category       TEXT,
        UNIQUE (trigger_id, headline_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_replay_mentions_trigger
    ON replay.trigger_mentions (trigger_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_replay_mentions_headline
    ON replay.trigger_mentions (headline_id)
    """,

    # Mirrors public.trigger_outcomes. References replay.triggers, NOT
    # public.triggers — these outcomes describe replay events.
    """
    CREATE TABLE IF NOT EXISTS replay.trigger_outcomes (
        id                  SERIAL PRIMARY KEY,
        trigger_id          INTEGER NOT NULL REFERENCES replay.triggers(id) ON DELETE CASCADE,
        trigger_fired_at    TIMESTAMPTZ NOT NULL,
        asset               TEXT NOT NULL,
        horizon_minutes     INTEGER NOT NULL,
        target_at           TIMESTAMPTZ NOT NULL,
        status              TEXT NOT NULL DEFAULT 'pending',
        return_pct          DOUBLE PRECISION,
        price_at_fire       DOUBLE PRECISION,
        price_at_target     DOUBLE PRECISION,
        resolved_at         TIMESTAMPTZ,
        attempts            INTEGER NOT NULL DEFAULT 0,
        last_attempt_at     TIMESTAMPTZ,
        last_error          TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (trigger_id, asset, horizon_minutes)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_replay_outcomes_pending_target
    ON replay.trigger_outcomes (target_at)
    WHERE status = 'pending'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_replay_outcomes_completed
    ON replay.trigger_outcomes (asset, horizon_minutes, trigger_id)
    WHERE status = 'completed'
    """,

    # Bookkeeping: where the walker has advanced to. We track by
    # (published_at, headline_id) tuple because published_at can have
    # ties and we need a deterministic resume point.
    """
    CREATE TABLE IF NOT EXISTS replay.replay_progress (
        id                          SERIAL PRIMARY KEY,
        run_id                      TEXT NOT NULL,
        started_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_processed_published_at TIMESTAMPTZ,
        last_processed_headline_id  INTEGER,
        completed_count             INTEGER NOT NULL DEFAULT 0,
        finished_at                 TIMESTAMPTZ,
        notes                       TEXT
    )
    """,
]

CHECK_QUERIES = {
    "replay schema":
        "SELECT 1 FROM information_schema.schemata WHERE schema_name='replay'",
    "replay.triggers":
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='replay' AND table_name='triggers'",
    "replay.trigger_mentions":
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='replay' AND table_name='trigger_mentions'",
    "replay.trigger_outcomes":
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='replay' AND table_name='trigger_outcomes'",
    "replay.replay_progress":
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='replay' AND table_name='replay_progress'",
}

# Drop SQL — used by the --drop flag for "start over" workflows.
DROP_SQL = "DROP SCHEMA IF EXISTS replay CASCADE"


def check(conn=None) -> dict[str, bool]:
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


def drop(conn=None) -> None:
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        cur = get_cursor(conn)
        logger.warning("Dropping replay schema and ALL data within it.")
        cur.execute(DROP_SQL)
        if owned:
            conn.commit()
        logger.info("Replay schema dropped.")
    finally:
        if owned:
            conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="replay schema migration")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--check", action="store_true",
                     help="Report current schema state, do not modify.")
    grp.add_argument("--drop", action="store_true",
                     help="DESTROY the replay schema and all its data. "
                          "Use this when you want a clean slate before "
                          "re-running the replay. Cannot be undone.")
    args = parser.parse_args()

    if args.check:
        state = check()
        print("Current replay-schema state:")
        for name, exists in state.items():
            mark = "✓" if exists else "✗"
            print(f"  {mark} {name}")
        return 0 if all(state.values()) else 1

    if args.drop:
        confirm = input(
            "This will DROP the replay schema and ALL its data "
            "(replay.triggers, replay.trigger_mentions, "
            "replay.trigger_outcomes, replay.replay_progress). "
            "Type 'yes' to proceed: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 1
        drop()
        return 0

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
