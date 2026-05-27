"""
Schema migration: add alerts table.

The alerts table is the durable contract between news-classifier (this
project, the producer) and crypto-yield-management-system (the consumer).
News-classifier inserts a row whenever the live pipeline decides to
tighten gates on a headline. The consumer subscribes to the `alerts_new`
NOTIFY channel and polls / fetches by id.

Design contract:

  - Loose append-only. News-classifier ONLY inserts; never updates or
    deletes. The consumer may update `consumed_at` and `acked_by` to
    track its own processing state. No other column is mutated after
    insert. Not enforced by privileges in this migration — convention.

  - Denormalised. Headline title, trigger signature, display_name, and
    Stage 2 outputs are copied onto the alert row at insert time. The
    consumer can do its job without ever joining triggers or headlines.
    Denormalisation also means an alert preserves its state at fire
    time — even if the underlying trigger's display_name later changes
    as new mentions arrive, the alert keeps the name that was alerted on.

  - NOTIFY via trigger function, not application code. Inserts go
    through `notify_alert_inserted()` which broadcasts the new id on
    the `alerts_new` channel. Application code can't forget to notify.
    Payload is just the id; the consumer SELECTs the row.

  - One alert per (headline_id). A single headline that crosses the
    tighten threshold produces exactly one alert. If live.py reprocesses
    the same headline (after a crash, say), the unique constraint
    suppresses the duplicate. The consumer can rely on each alert being
    a distinct event.

  - trigger_id is nullable. Rare path: classifier says
    should_tighten_gates=TRUE but the trigger system can't form a
    trigger (category not tracked, headline too generic). We still
    record the alert; the consumer sees trigger_id=NULL.

Idempotent. Run with:
    python -m config.migrate_alerts        # apply
    python -m config.migrate_alerts --check # report state, no changes
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
    # Main alerts table.
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id                      SERIAL PRIMARY KEY,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        -- Source references (FK constraints — but trigger_id may be NULL)
        headline_id             INTEGER NOT NULL REFERENCES headlines(id) ON DELETE CASCADE,
        trigger_id              INTEGER REFERENCES triggers(id) ON DELETE SET NULL,
        -- Denormalised headline data (consumer doesn't have to join)
        headline_title          TEXT NOT NULL,
        headline_published_at   TIMESTAMPTZ,
        -- Denormalised trigger data, snapshotted at fire time
        trigger_signature       TEXT,
        trigger_display_name    TEXT,
        category                TEXT NOT NULL,    -- classifier's category
        -- Stage 2 outputs
        direction               TEXT,             -- escalation/de-escalation/etc
        verb_category           TEXT,             -- e.g. CLOSES, IMPOSES
        -- Trigger state at fire time (snapshot, not live)
        is_novel                BOOLEAN NOT NULL,
        is_direction_change     BOOLEAN NOT NULL DEFAULT FALSE,
        impact_score            DOUBLE PRECISION,
        mention_count           INTEGER,
        -- Phase 5d will populate these. JSONB so the asset/horizon
        -- structure can evolve without further migrations. Format:
        -- {"BTCUSDT": {"60": {"mean": -0.4, "n": 14, "std": 1.2},
        --              "240": {...}, "1440": {...}},
        --  "ETHUSDT": {...}}
        priors                  JSONB,
        -- Consumer-writable columns. Producer never touches these
        -- after the initial INSERT (which leaves them NULL).
        consumed_at             TIMESTAMPTZ,
        acked_by                TEXT,
        -- Single-headline-one-alert; suppresses duplicates if live.py
        -- reprocesses a headline.
        UNIQUE (headline_id)
    )
    """,
    # Index supporting the consumer's catch-up query: "give me unprocessed
    # alerts in id order". Partial — only the unconsumed subset is indexed,
    # which keeps it small (consumer should drain regularly).
    """
    CREATE INDEX IF NOT EXISTS idx_alerts_unconsumed
    ON alerts (id)
    WHERE consumed_at IS NULL
    """,
    # Index for "alerts in this time window" consumer queries.
    """
    CREATE INDEX IF NOT EXISTS idx_alerts_created_at
    ON alerts (created_at DESC)
    """,
    # NOTIFY function. Broadcasts the new id on the `alerts_new`
    # channel. Payload is intentionally just the id — the consumer
    # SELECTs the row to get the data, so we don't have to keep the
    # notify payload in sync with schema changes. pg_notify's payload
    # is limited to ~8KB anyway; passing JSON would couple us to that
    # limit unnecessarily.
    """
    CREATE OR REPLACE FUNCTION notify_alert_inserted()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        PERFORM pg_notify('alerts_new', NEW.id::text);
        RETURN NEW;
    END;
    $$
    """,
    # The trigger itself. AFTER INSERT so the row is visible to a
    # SELECT-by-id by the time the consumer receives the NOTIFY.
    # (NOTIFYs are buffered until COMMIT, so the visibility ordering
    # is naturally correct, but AFTER INSERT keeps the intent clear.)
    """
    DROP TRIGGER IF EXISTS trg_alerts_notify ON alerts
    """,
    """
    CREATE TRIGGER trg_alerts_notify
    AFTER INSERT ON alerts
    FOR EACH ROW
    EXECUTE FUNCTION notify_alert_inserted()
    """,
]

CHECK_QUERIES = {
    "alerts table":
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name='alerts'",
    "alerts.priors":
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='alerts' AND column_name='priors'",
    "alerts.consumed_at":
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='alerts' AND column_name='consumed_at'",
    "idx_alerts_unconsumed":
        "SELECT 1 FROM pg_indexes "
        "WHERE indexname='idx_alerts_unconsumed'",
    "idx_alerts_created_at":
        "SELECT 1 FROM pg_indexes "
        "WHERE indexname='idx_alerts_created_at'",
    "notify_alert_inserted function":
        "SELECT 1 FROM pg_proc WHERE proname='notify_alert_inserted'",
    "trg_alerts_notify trigger":
        "SELECT 1 FROM pg_trigger "
        "WHERE tgname='trg_alerts_notify' AND tgrelid='alerts'::regclass",
}

# For manual rollback. Not run by default — irreversible data loss.
DOWN_SQL = """
-- Rollback (DESTROYS alert data — review before running):
DROP TRIGGER IF EXISTS trg_alerts_notify ON alerts;
DROP FUNCTION IF EXISTS notify_alert_inserted();
DROP INDEX IF EXISTS idx_alerts_created_at;
DROP INDEX IF EXISTS idx_alerts_unconsumed;
DROP TABLE IF EXISTS alerts;
"""


def check(conn=None) -> dict[str, bool]:
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        cur = get_cursor(conn)
        result = {}
        for name, q in CHECK_QUERIES.items():
            try:
                cur.execute(q)
                result[name] = cur.fetchone() is not None
            except Exception:
                # `tgrelid='alerts'::regclass` errors if `alerts` doesn't
                # exist yet. Treat as not-yet-applied rather than a hard
                # failure during pre-apply checking.
                conn.rollback()
                result[name] = False
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


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="alerts schema migration")
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
