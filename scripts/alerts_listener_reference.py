"""
Reference alert consumer (LISTEN/NOTIFY).

This is an EXAMPLE implementation of the consumer side of the alerts
contract. It belongs in crypto-yield-management-system, not in
news-classifier — but it lives here as reference so the protocol is
documented in one place.

Protocol:

  1. Consumer connects to the same Postgres instance as news-classifier
     and runs `LISTEN alerts_new`.

  2. When news-classifier inserts a row into `alerts`, a Postgres
     trigger fires `pg_notify('alerts_new', new_id::text)`. The
     payload is just the alert id.

  3. Consumer receives the notification, SELECTs the alert row by id,
     and processes it.

  4. After processing, consumer writes its identifier and timestamp
     to the `consumed_at` and `acked_by` columns. News-classifier
     never touches these.

  5. On startup or after a long disconnect, consumer should run a
     catch-up query to pick up any alerts that fired while it was
     offline:
        SELECT * FROM alerts
         WHERE consumed_at IS NULL
         ORDER BY id
     This is supported by the partial index `idx_alerts_unconsumed`.

Failure modes the consumer must handle:

  - NOTIFY can be dropped under load (Postgres won't queue infinite
    notifications). The consumer must NOT rely on every NOTIFY
    arriving. The catch-up query covers gaps.

  - Postgres restarts drop pending notifications. Same fix.

  - The NOTIFY arrives BEFORE the inserting transaction commits.
    Actually, Postgres guarantees the opposite: notifications are
    delivered after commit, so a SELECT-by-id will always find the
    row. But socket-level buffering means there's a small window
    where the consumer sees a notification before the row's WAL is
    flushed to a hot standby. Consumers reading from a replica
    should wait briefly or retry on missing rows.

  - Multiple consumers. Postgres LISTEN/NOTIFY broadcasts to all
    listeners. If two consumers want to share the workload (rather
    than both processing every alert), they need their own
    coordination — e.g. SELECT FOR UPDATE SKIP LOCKED on the
    catch-up query, claiming alerts atomically. Single-consumer is
    simpler; recommend that until you have a reason to scale out.

Usage (as standalone script for testing the producer end-to-end):

    python -m scripts.alerts_listener_reference

This prints alert ids and content as they arrive, but does NOT mark
them consumed. Useful as a "is the producer working" smoke test.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import select
import sys
from datetime import datetime, timezone

# Allow running as `python -m scripts.alerts_listener_reference` from
# project root (path setup mirrors the other scripts in this repo).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# psycopg2 is the standard PostgreSQL adapter; psycopg3 has slightly
# different LISTEN/NOTIFY ergonomics. We code against psycopg2 because
# that's what config.database in this project uses; the consumer
# project is free to choose differently.
try:
    import psycopg2
    import psycopg2.extensions
    import psycopg2.extras
except ImportError:
    print(
        "psycopg2 not available. Install: pip install psycopg2-binary",
        file=sys.stderr,
    )
    raise

# Use the project's existing connection helper. This reads from .env
# the same way every other script in news-classifier does, so there's
# only one place to configure the DB.
#
# When this code is adapted into crypto-yield-management-system, replace
# this import with that project's equivalent connection helper. DO NOT
# inline psycopg2.connect(...) — managing two ways of resolving DB
# credentials in one stack is a recipe for the exact "no password
# supplied" error that motivated this change.
from config.database import get_connection

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("alerts_listener")


CHANNEL = "alerts_new"


def _open_listening_connection():
    """Open a connection in autocommit mode with LISTEN issued.

    Autocommit is required for LISTEN/NOTIFY — notifications are not
    delivered to a connection mid-transaction. The cursor itself is
    short-lived; we re-issue cursors per query.

    Uses the project's get_connection() and then sets autocommit. The
    connection helper applies whatever auth/host config the rest of
    the project uses (currently .env via python-dotenv), so this
    listener can't drift out of sync with how live.py connects.
    """
    conn = get_connection()
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"LISTEN {CHANNEL}")
    cur.close()
    logger.info("Listening on channel '%s'", CHANNEL)
    return conn


def _fetch_alert_by_id(conn, alert_id: int) -> dict | None:
    """SELECT the full alert row by id. Returns None if not found
    (which would be unusual — the producer commits the row before
    the trigger fires the NOTIFY)."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, created_at, headline_id, trigger_id,
               headline_title, headline_published_at,
               trigger_signature, trigger_display_name, category,
               direction, verb_category,
               is_novel, is_direction_change,
               impact_score, mention_count,
               priors,
               consumed_at, acked_by
        FROM alerts
        WHERE id = %s
    """, (alert_id,))
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def _fetch_unconsumed_catchup(conn, limit: int = 100) -> list[dict]:
    """Catch-up query: fetch alerts that haven't been consumed yet.

    Run on startup before entering the LISTEN loop, and periodically
    as a belt-and-braces safety net against dropped NOTIFYs.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id FROM alerts
        WHERE consumed_at IS NULL
        ORDER BY id
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    cur.close()
    return [r["id"] for r in rows]


def _mark_consumed(conn, alert_id: int, acked_by: str) -> None:
    """Write back consumed_at / acked_by. The producer never touches
    these columns — only consumers update them."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE alerts
        SET consumed_at = NOW(), acked_by = %s
        WHERE id = %s
    """, (acked_by, alert_id))
    cur.close()


def _handle_alert(alert: dict) -> None:
    """REPLACE THIS in your consumer with whatever the trading logic does.
    The reference implementation just pretty-prints the alert."""
    print(json.dumps({
        "id": alert["id"],
        "created_at": alert["created_at"].isoformat()
                       if alert.get("created_at") else None,
        "headline_title": alert.get("headline_title"),
        "trigger_signature": alert.get("trigger_signature"),
        "direction": alert.get("direction"),
        "verb_category": alert.get("verb_category"),
        "is_novel": alert.get("is_novel"),
        "impact_score": alert.get("impact_score"),
        "priors": alert.get("priors"),
    }, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--ack-as", default="reference-listener",
        help="Identifier to write to acked_by when marking consumed "
             "(default: 'reference-listener'). Use the consumer "
             "service name in production.",
    )
    parser.add_argument(
        "--no-ack", action="store_true",
        help="Do NOT mark alerts consumed. Useful for tail-style "
             "observation without disturbing other consumers.",
    )
    parser.add_argument(
        "--catchup-only", action="store_true",
        help="Process pending unconsumed alerts and exit; do not LISTEN.",
    )
    args = parser.parse_args()

    conn = _open_listening_connection()

    # Catch-up: drain unconsumed alerts from before we connected.
    pending = _fetch_unconsumed_catchup(conn)
    if pending:
        logger.info("Catching up on %d unconsumed alert(s)", len(pending))
        for alert_id in pending:
            alert = _fetch_alert_by_id(conn, alert_id)
            if alert is None:
                logger.warning("Catch-up: alert %s missing", alert_id)
                continue
            _handle_alert(alert)
            if not args.no_ack:
                _mark_consumed(conn, alert_id, args.ack_as)

    if args.catchup_only:
        return 0

    # LISTEN loop. select.select() blocks until the socket has data
    # (a NOTIFY message). We poll notifies off conn.notifies after
    # each wakeup and clear it.
    logger.info("Entering LISTEN loop (Ctrl-C to exit)")
    try:
        while True:
            if select.select([conn], [], [], 60) == ([], [], []):
                # 60s timeout — heartbeat. Could do periodic catch-up
                # here as a NOTIFY-loss safety net, but for the
                # reference listener we just keep going.
                continue
            conn.poll()
            while conn.notifies:
                notify = conn.notifies.pop(0)
                try:
                    alert_id = int(notify.payload)
                except ValueError:
                    logger.warning(
                        "Unexpected non-integer NOTIFY payload: %r",
                        notify.payload,
                    )
                    continue
                alert = _fetch_alert_by_id(conn, alert_id)
                if alert is None:
                    # Could happen if alert was deleted (CASCADE on
                    # headline delete) between NOTIFY and our SELECT.
                    logger.warning("Alert %s gone before fetch", alert_id)
                    continue
                _handle_alert(alert)
                if not args.no_ack:
                    _mark_consumed(conn, alert_id, args.ack_as)
    except KeyboardInterrupt:
        logger.info("Exiting cleanly")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
