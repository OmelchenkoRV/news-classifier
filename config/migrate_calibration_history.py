"""
Schema migration: calibration history (Phase 6 diagnostic).

⚠ TEMPORARY FEATURE — meant to be torn down once forward collection
yields a verdict on prior stability.

This pair of tables stores per-run and per-cell results of the
forecaster's calibration so you can track sign-agreement and prior
magnitudes over time. The intent is a 60-90 day diagnostic: does
any cell's sign-agreement stabilize above 60% as more data
accumulates?

Schema (two tables):

  calibration_runs       — one row per calibration invocation
                           (aggregate_by, metric, train_frac, summary
                           statistics, notes)

  calibration_cells      — one row per cell per run (bucket, direction,
                           asset, horizon, n_train/test, prior, test
                           actuals, sign_agreement)

Two tables instead of one because most queries for trend analysis
filter by cell first ("show me geopolitical:escalation:60min over
time") and then join run-level metadata only when needed. Storing
run-level summaries once per run avoids duplicating them across
50-100 cell rows per run.

Lifecycle:
  - Apply this migration to start collecting.
  - The calibration-history docker-compose service runs once daily.
  - After the diagnostic period, when you've made the call on whether
    priors are predictive, drop the whole feature:
      1. Stop and remove the calibration-history service from
         docker-compose.yml
      2. Run: python -m config.migrate_calibration_history --drop
      3. Remove this file, scripts/calibrate_history.py, and the
         --store/--notes/--quiet flags from calibrate_forecaster.py
      4. Remove this migration from entrypoint.sh

Usage:
    python -m config.migrate_calibration_history          # apply
    python -m config.migrate_calibration_history --check  # status
    python -m config.migrate_calibration_history --drop   # tear down

Idempotent on apply. Drop is destructive (with a confirmation prompt).
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
    """
    CREATE TABLE IF NOT EXISTS calibration_runs (
        id                          SERIAL PRIMARY KEY,
        run_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        aggregate_by                TEXT NOT NULL,
        metric                      TEXT NOT NULL,
        train_frac                  DOUBLE PRECISION NOT NULL,
        min_cell_n                  INTEGER NOT NULL,
        schemas                     TEXT[] NOT NULL,
        train_n                     INTEGER NOT NULL,
        test_n                      INTEGER NOT NULL,
        n_cells_evaluated           INTEGER NOT NULL,
        aggregate_sign_agreement    DOUBLE PRECISION,
        cells_above_55_pct          INTEGER NOT NULL DEFAULT 0,
        cells_above_60_pct          INTEGER NOT NULL DEFAULT 0,
        notes                       TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_calruns_run_at
    ON calibration_runs (run_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_calruns_variant
    ON calibration_runs (aggregate_by, metric, run_at DESC)
    """,

    """
    CREATE TABLE IF NOT EXISTS calibration_cells (
        id                  SERIAL PRIMARY KEY,
        run_id              INTEGER NOT NULL REFERENCES calibration_runs(id) ON DELETE CASCADE,
        bucket              TEXT NOT NULL,
        direction           TEXT,
        asset               TEXT NOT NULL,
        horizon_minutes     INTEGER NOT NULL,
        n_train             INTEGER NOT NULL,
        n_test              INTEGER NOT NULL,
        prior_mean          DOUBLE PRECISION,
        prior_std           DOUBLE PRECISION,
        test_actual_mean    DOUBLE PRECISION,
        test_actual_std     DOUBLE PRECISION,
        mae                 DOUBLE PRECISION,
        sign_agreement      DOUBLE PRECISION
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_calcells_run
    ON calibration_cells (run_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_calcells_cell
    ON calibration_cells (bucket, direction, asset, horizon_minutes, run_id)
    """,
]

DROP_SQL = [
    "DROP TABLE IF EXISTS calibration_cells CASCADE",
    "DROP TABLE IF EXISTS calibration_runs CASCADE",
]

CHECK_QUERIES = {
    "calibration_runs":
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='calibration_runs'",
    "calibration_cells":
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='calibration_cells'",
    "idx_calruns_run_at":
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_calruns_run_at'",
    "idx_calruns_variant":
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_calruns_variant'",
    "idx_calcells_run":
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_calcells_run'",
    "idx_calcells_cell":
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_calcells_cell'",
}


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
            logger.info("Executing: %s", stmt.strip().split("\n")[0][:80])
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
        logger.warning("Dropping calibration history tables and ALL data.")
        for stmt in DROP_SQL:
            cur.execute(stmt)
        if owned:
            conn.commit()
        logger.info("Calibration history dropped.")
    finally:
        if owned:
            conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="calibration history migration")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--check", action="store_true",
                     help="Report current state, do not modify.")
    grp.add_argument("--drop", action="store_true",
                     help="DESTROY calibration_runs + calibration_cells "
                          "and all their data. Use this when you've "
                          "concluded the diagnostic period and are "
                          "removing the feature. Cannot be undone.")
    args = parser.parse_args()

    if args.check:
        state = check()
        print("Calibration history state:")
        for name, exists in state.items():
            mark = "✓" if exists else "✗"
            print(f"  {mark} {name}")
        return 0 if all(state.values()) else 1

    if args.drop:
        confirm = input(
            "This will DROP calibration_runs and calibration_cells "
            "and all their data. If you also want to remove the cron "
            "service, edit docker-compose.yml manually. "
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
        print("WARNING: post-apply state has missing artefacts:")
        for name, exists in state.items():
            if not exists:
                print(f"  ✗ {name}")
        return 1
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
