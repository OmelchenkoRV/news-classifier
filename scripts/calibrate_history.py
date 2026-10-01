"""
Calibration history runner (Phase 6 diagnostic, TEMPORARY).

Runs the four calibration variants in sequence and persists each to
calibration_runs + calibration_cells. This is the long-running cron
service for the diagnostic period — once a day it captures the
forecaster's predictive behaviour at that moment, so over 60-90 days
you can chart whether any cells stabilise.

Variants captured per cycle:
    1. signature_root × return_pct        (production prior, raw)
    2. signature_root × excess_return_pct (production prior, detrended)
    3. category × return_pct              (diagnostic, raw)
    4. category × excess_return_pct       (diagnostic, detrended)

Four `calibration_runs` rows per cycle, plus 50-200 `calibration_cells`
rows total (depending on data volume). At daily cadence, ~1500 runs
per year, ~50000 cells. Trivially small; no retention pressure.

Each variant is run as a SUBPROCESS rather than imported, so a crash
in one variant doesn't abort the others. Same as how cron would
invoke them.

This script is meant to be the entrypoint of the
calibration-history docker-compose service:

    while true; do
        python -m scripts.calibrate_history
        sleep 86400
    done

Or run manually for a one-shot capture:

    python -m scripts.calibrate_history

Failures don't abort the loop. If a variant fails (e.g., not enough
data for a given metric/aggregation combo), it logs and moves on.

⚠ TEMPORARY — see config/migrate_calibration_history.py for the
teardown checklist.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("calibrate_history")


# (aggregate_by, metric) variants captured per cycle. Order isn't
# important — each is independent. Storing all four lets you compare
# raw vs detrended at the same moment, which is the only honest way
# to read whether detrending is helping.
VARIANTS = [
    ("signature_root", "return_pct"),
    ("signature_root", "excess_return_pct"),
    ("category", "return_pct"),
    ("category", "excess_return_pct"),
]


def run_variant(aggregate_by: str, metric: str, notes: str) -> bool:
    """Run a single calibration variant. Returns True on success."""
    cmd = [
        sys.executable, "-m", "scripts.calibrate_forecaster",
        "--aggregate-by", aggregate_by,
        "--metric", metric,
        "--store",
        "--quiet",
        "--notes", notes,
    ]
    logger.info("Running variant: %s × %s", aggregate_by, metric)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.error("Variant %s × %s timed out after 5min",
                     aggregate_by, metric)
        return False
    except Exception as e:
        logger.error("Variant %s × %s failed to launch: %s",
                     aggregate_by, metric, e)
        return False

    if result.returncode != 0:
        # Non-zero exit means the calibrator itself failed (e.g., no
        # outcomes for this metric, or DB error). Log stderr and
        # continue — the other variants may still succeed.
        logger.warning(
            "Variant %s × %s returned %d. stderr: %s",
            aggregate_by, metric, result.returncode,
            result.stderr.strip()[:500],
        )
        return False

    logger.info("Variant %s × %s ok", aggregate_by, metric)
    return True


def run_cycle() -> dict:
    """One full cycle: run all four variants. Returns counters."""
    started = time.time()
    notes = f"automated calibration-history cycle {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
    stats = {"ok": 0, "failed": 0}
    for aggregate_by, metric in VARIANTS:
        if run_variant(aggregate_by, metric, notes):
            stats["ok"] += 1
        else:
            stats["failed"] += 1
    elapsed = time.time() - started
    logger.info(
        "Cycle complete: ok=%d failed=%d elapsed=%.1fs",
        stats["ok"], stats["failed"], elapsed,
    )
    return stats


def main() -> int:
    """One-shot invocation. The docker-compose service's shell loop
    handles the daily cadence and sleep. Keeps this script trivial
    and matches the price-backfill pattern."""
    stats = run_cycle()
    # Exit 0 even if some variants failed — the loop continues. The
    # operator sees failures in logs.
    return 0


if __name__ == "__main__":
    sys.exit(main())
