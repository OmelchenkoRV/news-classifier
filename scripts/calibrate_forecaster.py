"""
Forecaster calibration: chronological train/test split.

Evaluates whether the priors computed by pipeline.forecaster are actually
predictive, or noise around zero. The whole point of Phase 5 stands or
falls on this answer:

  - If priors are predictive (test-set outcomes correlate with their
    predicted means, with a meaningful effect size), surface them in
    alerts and we have a useful signal.

  - If priors are noise, we know not to trust them. Better to find out
    here than after a quarter of trading on garbage signals.

Methodology:

  1. Fetch all completed outcomes from public + replay schemas.
  2. Sort chronologically by trigger_fired_at.
  3. Split first 80% chronologically (train) from last 20% (test).
  4. Build aggregated priors from the train set only.
  5. For each test outcome, look up its (signature_root, direction,
     asset, horizon) in the train priors. If a prior exists, compare
     the prior's mean against the test outcome's actual return.
  6. Report per-cell statistics: hit rate of sign agreement, mean
     absolute error of the predicted mean, n.

A simplification worth flagging: this approach uses the FULL train set
to build priors, then evaluates against ALL test outcomes. A stricter
backtest would compute priors "as of each test outcome's fire time"
using only data available before that moment. The stricter version
would prevent the (small) leakage where late-train data near the
train/test boundary informs early-test predictions. If the simpler
version says priors are noise, the stricter version would say so even
more clearly — so the simpler check is sufficient for a go/no-go.
The forecaster itself uses as-of-fire-time semantics in production
(see priors_for_trigger's `before` parameter); the simplification is
only here in the calibration tool.

Output:

  Per-cell table sorted by n (descending) — the most-active cells are
  the most informative. Each row shows:
    n_train, n_test, prior_mean, test_actual_mean, mae,
    sign_agreement_rate, p_value (sign test against null:
    "prior is uninformative")

Sign-agreement is the simplest possible directional check. For each
test outcome, did the prior mean and the actual return have the same
sign? Random would give 50%. We want >55% to call a cell predictive.
A stricter shop would compute correlation coefficients or KS tests
against the null distribution; this is a first-cut diagnostic.

Usage:

    python -m scripts.calibrate_forecaster

    python -m scripts.calibrate_forecaster --json   # for piping/dashboards
    python -m scripts.calibrate_forecaster --min-cell-n 10  # tighter
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
from collections import defaultdict
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import get_connection
from pipeline.forecaster import (
    fetch_all_outcomes, _strip_direction_suffix,
    aggregate_priors, MIN_N, DEFAULT_SCHEMAS,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("calibrate")


# Default split: 80% train / 20% test, chronological.
DEFAULT_TRAIN_FRAC: float = 0.80


def _split_chronological(rows: list[dict], train_frac: float
                         ) -> tuple[list[dict], list[dict]]:
    """Sort by trigger_fired_at, return (train, test)."""
    rows_sorted = sorted(rows, key=lambda r: r["trigger_fired_at"])
    cut = int(len(rows_sorted) * train_frac)
    return rows_sorted[:cut], rows_sorted[cut:]


def _evaluate(train: list[dict], test: list[dict],
              min_cell_n: int,
              key_fn=None,
              metric: str = "return_pct") -> list[dict]:
    """Evaluate test outcomes against train-set priors. Returns one row
    per cell that has at least min_cell_n train outcomes AND at least
    1 test outcome.

    `key_fn` controls aggregation granularity. None = use the
    forecaster's default (signature_root, direction, asset, horizon).
    Pass forecaster.category_key_fn to aggregate by category instead
    — useful when the data is too thin for per-topic aggregation.

    `metric` selects which return column to evaluate. 'return_pct' for
    raw return; 'excess_return_pct' for detrended (excess over the
    asset's prior-window return).
    """
    from pipeline.forecaster import _default_key_fn
    if key_fn is None:
        key_fn = _default_key_fn

    train_priors = aggregate_priors(train, key_fn=key_fn, metric=metric)
    # Build test buckets parallel to train priors using the SAME key_fn
    # AND the same metric so they line up.
    test_buckets: dict = defaultdict(list)
    for row in test:
        value = row.get(metric)
        if value is None:
            continue
        key = key_fn(row)
        test_buckets[key].append(float(value))

    results = []
    for key, prior in train_priors.items():
        if prior.n < min_cell_n:
            continue
        test_returns = test_buckets.get(key, [])
        if not test_returns:
            continue

        # Sign agreement between prior mean and each test outcome.
        # Treat zero-mean priors specially — they predict no direction,
        # and "agreement" is undefined. Skip the agreement metric for them.
        if abs(prior.mean) < 1e-9:
            sign_agreement = None
        else:
            agreements = [
                1 if (actual * prior.mean) > 0 else 0
                for actual in test_returns
            ]
            sign_agreement = sum(agreements) / len(agreements)

        # Mean absolute error of the prior as a point predictor
        mae = statistics.mean(abs(actual - prior.mean) for actual in test_returns)

        # Test-set actuals summary
        test_mean = statistics.mean(test_returns)
        test_std = (statistics.stdev(test_returns)
                    if len(test_returns) >= 2 else 0.0)

        results.append({
            "bucket": key[0],
            "direction": key[1],
            "asset": key[2],
            "horizon_minutes": key[3],
            "n_train": prior.n,
            "n_test": len(test_returns),
            "prior_mean": prior.mean,
            "prior_std": prior.std,
            "test_actual_mean": test_mean,
            "test_actual_std": test_std,
            "mae": mae,
            "sign_agreement": sign_agreement,
        })

    # Sort by n_train descending — most-evidence cells first
    results.sort(key=lambda r: r["n_train"], reverse=True)
    return results


def _print_report(results: list[dict], train_n: int, test_n: int) -> None:
    print()
    print("=" * 100)
    print("FORECASTER CALIBRATION REPORT")
    print("=" * 100)
    print(f"Total outcomes:    {train_n + test_n}")
    print(f"Train set:         {train_n}")
    print(f"Test set:          {test_n}")
    print(f"Cells evaluated:   {len(results)}")
    print()

    if not results:
        print("✗ No cells had enough train AND test data to evaluate.")
        print("  Either you have very thin data, or the test window")
        print("  contains entirely different signature roots than train.")
        return

    # Header
    cols = (
        ("root × dir", 38),
        ("asset", 10),
        ("h", 4),
        ("n_tr", 5),
        ("n_te", 5),
        ("prior", 8),
        ("test", 8),
        ("mae", 7),
        ("sign%", 7),
    )
    print(" ".join(f"{name:<{w}}" for name, w in cols))
    print("-" * sum(w + 1 for _, w in cols))

    # One row per cell
    for r in results:
        bucket_dir = f"{r['bucket']}:{r['direction'] or '-'}"
        sign_str = (
            f"{r['sign_agreement']:.0%}" if r['sign_agreement'] is not None
            else "n/a"
        )
        print(
            f"{bucket_dir[:38]:<38} {r['asset']:<10} "
            f"{r['horizon_minutes']:<4} "
            f"{r['n_train']:<5} {r['n_test']:<5} "
            f"{r['prior_mean']:>+7.2f} "
            f"{r['test_actual_mean']:>+7.2f} "
            f"{r['mae']:>6.2f} "
            f"{sign_str:>6}"
        )

    # Summary across cells
    sign_results = [r["sign_agreement"] for r in results
                    if r["sign_agreement"] is not None]
    if sign_results:
        agg_sign = statistics.mean(sign_results)
        n_above_55 = sum(1 for s in sign_results if s > 0.55)
        n_above_60 = sum(1 for s in sign_results if s > 0.60)
        print()
        print(f"Aggregate sign-agreement (mean across cells): {agg_sign:.1%}")
        print(f"  Cells with sign-agreement > 55%: {n_above_55} / {len(sign_results)}")
        print(f"  Cells with sign-agreement > 60%: {n_above_60} / {len(sign_results)}")
        print()
        print("Interpretation:")
        print("  - Random sign agreement is 50%. >55% suggests informative.")
        print("  - >60% across many cells is strong evidence priors work.")
        print("  - Below 55% means priors are essentially noise; don't surface.")
        if agg_sign < 0.50:
            print()
            print("⚠ Aggregate sign-agreement is BELOW 50% — priors may be")
            print("  systematically anti-predictive. Investigate before")
            print("  surfacing in alerts.")


def _store_results(conn, args, results: list[dict],
                   train_n: int, test_n: int, notes: Optional[str] = None) -> int:
    """Write the calibration run + per-cell rows to the history tables.
    Returns the inserted run_id.

    Tables (calibration_runs, calibration_cells) must exist; created
    by config.migrate_calibration_history. If they don't, this will
    surface UndefinedTable rather than silently swallow."""
    cur = conn.cursor()

    # Aggregate metrics for the run-level row.
    sign_results = [r["sign_agreement"] for r in results
                    if r["sign_agreement"] is not None]
    aggregate_sign = (statistics.mean(sign_results) if sign_results
                      else None)
    cells_above_55 = sum(1 for s in sign_results if s > 0.55)
    cells_above_60 = sum(1 for s in sign_results if s > 0.60)

    cur.execute("""
        INSERT INTO calibration_runs (
            aggregate_by, metric, train_frac, min_cell_n, schemas,
            train_n, test_n, n_cells_evaluated,
            aggregate_sign_agreement, cells_above_55_pct, cells_above_60_pct,
            notes
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        args.aggregate_by, args.metric, args.train_frac, args.min_cell_n,
        list(args.schemas),
        train_n, test_n, len(results),
        aggregate_sign, cells_above_55, cells_above_60,
        notes,
    ))
    run_id = cur.fetchone()[0]

    # Per-cell rows. Bulk-insert via executemany — small enough that
    # this is fine.
    cell_rows = [
        (
            run_id,
            r["bucket"],
            r["direction"],
            r["asset"],
            r["horizon_minutes"],
            r["n_train"],
            r["n_test"],
            r["prior_mean"],
            r["prior_std"],
            r["test_actual_mean"],
            r["test_actual_std"],
            r["mae"],
            r["sign_agreement"],
        )
        for r in results
    ]
    cur.executemany("""
        INSERT INTO calibration_cells (
            run_id, bucket, direction, asset, horizon_minutes,
            n_train, n_test, prior_mean, prior_std,
            test_actual_mean, test_actual_std, mae, sign_agreement
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, cell_rows)

    conn.commit()
    return run_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--train-frac", type=float, default=DEFAULT_TRAIN_FRAC,
        help=f"Fraction of outcomes used for training "
             f"(default: {DEFAULT_TRAIN_FRAC})",
    )
    parser.add_argument(
        "--min-cell-n", type=int, default=MIN_N,
        help=f"Minimum train-set outcomes required to evaluate a cell "
             f"(default: {MIN_N}). Higher = more conservative.",
    )
    parser.add_argument(
        "--schemas", nargs="+", default=list(DEFAULT_SCHEMAS),
        help=f"Schemas to read outcomes from (default: {list(DEFAULT_SCHEMAS)})",
    )
    parser.add_argument(
        "--aggregate-by", choices=["signature_root", "category"],
        default="signature_root",
        help="Aggregation granularity. 'signature_root' (default) buckets "
             "by (topic, direction, asset, horizon) — what production "
             "priors use, but requires recurring topics for cells to "
             "have enough data. 'category' buckets by "
             "(category, direction, asset, horizon) — diagnostic only, "
             "loses topic specificity but works on thin data.",
    )
    parser.add_argument(
        "--metric", choices=["return_pct", "excess_return_pct"],
        default="return_pct",
        help="Which return measure to evaluate. 'return_pct' (default) is "
             "raw return — the trigger's H-minute return. "
             "'excess_return_pct' is detrended — the excess of the "
             "trigger's return over the asset's prior-window return. "
             "Detrended priors filter out market drift and are usually "
             "more honest, but smaller in magnitude.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of human-readable table.",
    )
    parser.add_argument(
        "--store", action="store_true",
        help="Persist this run's results to calibration_runs and "
             "calibration_cells tables. Used by the calibration-history "
             "cron service to track how priors evolve over time. Requires "
             "config.migrate_calibration_history to have been applied.",
    )
    parser.add_argument(
        "--notes", type=str, default=None,
        help="Optional free-form annotation written to the run's "
             "notes field when --store is set. Useful for marking "
             "context like 'first run after Iran cluster ended'.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress the human-readable report. Useful for cron "
             "runs where you only care about persistence.",
    )
    args = parser.parse_args()

    if not (0.1 <= args.train_frac <= 0.95):
        logger.error("--train-frac must be between 0.1 and 0.95")
        return 1

    # Resolve key_fn from CLI flag.
    from pipeline.forecaster import _default_key_fn, category_key_fn
    key_fn = (_default_key_fn if args.aggregate_by == "signature_root"
              else category_key_fn)

    conn = get_connection()
    try:
        rows = fetch_all_outcomes(conn, schemas=tuple(args.schemas))
        # Filter to rows with the chosen metric populated. For
        # excess_return_pct, this can be more restrictive — outcomes
        # without a baseline (no candle at fire-H) get filtered out
        # here rather than dragging null values into aggregation.
        rows = [r for r in rows if r.get(args.metric) is not None]

        if not rows:
            logger.error(
                "No completed outcomes with %s found in schemas %s. "
                "If using excess_return_pct, you may need to run "
                "scripts.backfill_detrending first.",
                args.metric, args.schemas,
            )
            return 1

        train, test = _split_chronological(rows, args.train_frac)
        results = _evaluate(train, test, args.min_cell_n,
                            key_fn=key_fn, metric=args.metric)

        if args.store:
            run_id = _store_results(
                conn, args, results,
                train_n=len(train), test_n=len(test),
                notes=args.notes,
            )
            logger.info("Stored calibration run id=%s", run_id)

        if args.json:
            print(json.dumps({
                "train_n": len(train),
                "test_n": len(test),
                "aggregate_by": args.aggregate_by,
                "metric": args.metric,
                "cells": results,
            }, indent=2, default=str))
        elif not args.quiet:
            _print_report(results, len(train), len(test))

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
