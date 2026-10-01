"""
Outcome worker: resolve pending trigger_outcomes.

Designed to run as either:
  - A scheduled cron (every minute is fine; the worker is idempotent)
  - A one-shot invocation after a backfill or replay
  - Continuously with --loop for foreground operation

The worker reads pending outcomes whose target_at has passed, looks up
the close prices in price_snapshots, computes return percentage, and
writes the result back. See pipeline/outcomes.py for the actual logic;
this script is a thin CLI wrapper.

Usage:
    python -m scripts.outcomes_worker                     # one pass
    python -m scripts.outcomes_worker --batch 500         # bigger batch
    python -m scripts.outcomes_worker --loop --interval 60  # foreground daemon
    python -m scripts.outcomes_worker --json              # machine-readable

Replay mode: drain the replay schema's pending outcomes against the
SAME public.price_snapshots table. Useful after running
scripts.replay_headlines:

    python -m scripts.outcomes_worker --schema replay --batch 5000

Exit code: 0 on success (even if some outcomes deferred), 1 on fatal error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import get_connection
from pipeline.outcomes import resolve_pending

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("outcomes_worker")


def _print_stats(stats, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(asdict(stats)))
    else:
        print(
            f"scanned={stats.scanned} completed={stats.completed} "
            f"unavailable={stats.unavailable} deferred={stats.deferred} "
            f"errors={stats.errors}"
        )


def _open_conn_with_schema(schema: str | None):
    """Open a connection and (optionally) point its search_path at a
    non-default schema. The worker uses this when --schema is set so
    resolve_pending operates on replay.trigger_outcomes etc. rather
    than public.trigger_outcomes.

    price_snapshots only exists in public, so we always include public
    in the search_path. Order matters: the named schema wins for any
    table present in both."""
    conn = get_connection()
    if schema is not None and schema != "public":
        # Use psycopg2.sql.Identifier rather than string formatting —
        # SET search_path doesn't accept parameterised values via %s,
        # and naive string interpolation would be a SQL injection
        # vector. Identifier handles quoting/escaping correctly.
        from psycopg2 import sql
        cur = conn.cursor()
        cur.execute(sql.SQL("SET search_path TO {}, public").format(
            sql.Identifier(schema)
        ))
        cur.close()
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--batch", type=int, default=200,
        help="Max outcomes to resolve per pass (default: 200).",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously, sleeping --interval seconds between passes.",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Sleep this many seconds between passes when --loop is set "
             "(default: 60).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON stats instead of human-readable text.",
    )
    parser.add_argument(
        "--schema", type=str, default=None,
        help="Schema to drain. Default: public. Pass 'replay' to drain "
             "the replay schema's pending outcomes after running "
             "scripts.replay_headlines. The worker reads price candles "
             "from public.price_snapshots regardless of this setting.",
    )
    args = parser.parse_args()

    fmt = "json" if args.json else "text"

    # Validate the schema name to avoid SQL injection via search_path.
    # Only allow alphanumeric + underscore, max 63 chars (Postgres limit).
    if args.schema is not None:
        if not args.schema.replace("_", "").isalnum() or len(args.schema) > 63:
            logger.error("Invalid --schema name: %r", args.schema)
            return 1

    try:
        if args.loop:
            logger.info(
                "Starting outcomes worker loop (schema=%s batch=%s interval=%ss)",
                args.schema or "public", args.batch, args.interval,
            )
            # Note: we open a fresh connection PER CYCLE rather than
            # holding one across the loop's lifetime. This was a
            # response to a real production failure: a 9-hour-old
            # connection silently degraded to the point where SELECT
            # queries returned empty results without raising. The
            # worker logged scanned=0 every cycle while 12 outcomes
            # sat past their target_at waiting to be resolved.
            #
            # The cost is negligible — opening a Postgres connection
            # is single-digit milliseconds, dwarfed by the 60-second
            # poll interval. The benefit is that any per-connection
            # state corruption resets every cycle, making this whole
            # class of bug impossible.
            #
            # If you need lower per-cycle latency in the future,
            # introduce a connection pool with explicit health checks
            # rather than a single long-lived connection.
            while True:
                cycle_conn = _open_conn_with_schema(args.schema)
                try:
                    stats = resolve_pending(conn=cycle_conn,
                                            batch_size=args.batch)
                    # CRITICAL: resolve_pending only commits when it
                    # owns the connection. We pass our own connection
                    # in so the search_path stays set, which means we
                    # MUST commit explicitly here.
                    cycle_conn.commit()
                    _print_stats(stats, fmt)
                except Exception as e:
                    logger.exception("Cycle failed: %s", e)
                    try:
                        cycle_conn.rollback()
                    except Exception:
                        pass
                finally:
                    try:
                        cycle_conn.close()
                    except Exception:
                        pass
                # Sleep regardless of success/failure. Bounded work-
                # rate is a feature, not a bug.
                time.sleep(args.interval)
        else:
            # Single-shot path: open one connection, do the work,
            # close. Same connection-per-invocation principle as the
            # loop, just without the loop wrapper.
            conn = _open_conn_with_schema(args.schema)
            try:
                stats = resolve_pending(conn=conn, batch_size=args.batch)
                conn.commit()
                _print_stats(stats, fmt)
            finally:
                conn.close()
        return 0
    except KeyboardInterrupt:
        logger.info("Interrupted; exiting cleanly.")
        return 0
    except Exception as e:
        logger.exception("Fatal error in outcomes worker: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

