"""
Trigger outcome scheduling and resolution.

Two responsibilities:

  1. Scheduling. When a trigger is created (or a re-replayed historical
     trigger fires in the replay pipeline), insert one `trigger_outcomes`
     row per (asset × horizon) combination, with status='pending' and
     target_at = trigger_fired_at + horizon. The worker picks these up.

  2. Resolution. The worker scans for pending outcomes whose target_at
     has passed, looks up close prices in `price_snapshots` at fire time
     and target time, computes return percentage, and writes the result
     back to the row.

Why split scheduling from resolution? They run at different cadences and
can fail independently. Scheduling is synchronous with trigger creation
(must succeed for outcome tracking to work). Resolution is asynchronous
and retryable (price data may not yet exist, network may flap, etc).

Design choices documented in config/migrate_outcomes.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.database import get_connection, get_cursor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration: which assets, which horizons.
#
# Edit here to extend. The scheduler emits one outcome row per combination,
# so adding a horizon adds N rows per trigger (one per asset).
#
# Horizons in MINUTES so we can express "1 hour" / "4 hours" / "24 hours"
# uniformly with arithmetic (and stay consistent with `target_at` math).
# ---------------------------------------------------------------------------
TRACKED_ASSETS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
TRACKED_HORIZONS_MIN: tuple[int, ...] = (60, 240, 1440)  # 1h, 4h, 24h

# Tolerance when matching a target timestamp to a price candle. The
# price_snapshots table has hourly OHLCV, so the worst-case mismatch
# is 30 minutes (target falls between two candles). We allow up to 90
# minutes to handle minor gaps in the candle history (e.g. a 1-2h
# Binance API outage on an old day) without giving up on the outcome.
# Beyond 90 minutes we mark the outcome `unavailable` to keep noise
# out of the forecaster.
PRICE_MATCH_TOLERANCE_MIN: int = 90

# After this many failed resolution attempts, give up and mark the
# outcome `unavailable`. Prevents the worker from retrying forever on
# a row that genuinely can't be resolved (e.g. trigger fired before
# our price history starts).
MAX_RESOLUTION_ATTEMPTS: int = 5


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------
@dataclass
class SchedulingResult:
    trigger_id: int
    rows_created: int
    rows_skipped: int  # already-existed (UNIQUE constraint)


@dataclass
class WorkerStats:
    scanned: int
    completed: int
    unavailable: int
    deferred: int      # left pending — will retry next pass
    errors: int


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
def schedule_outcomes(
    trigger_id: int,
    trigger_fired_at: datetime,
    conn=None,
    assets: tuple[str, ...] = TRACKED_ASSETS,
    horizons_min: tuple[int, ...] = TRACKED_HORIZONS_MIN,
) -> SchedulingResult:
    """Insert pending-outcome rows for a newly-created trigger.

    Idempotent: ON CONFLICT DO NOTHING means re-scheduling the same
    trigger is harmless (re-running the replay won't double-insert).

    Args:
        trigger_id: id of the trigger that fired.
        trigger_fired_at: when the *event* happened. For live triggers
            this is the headline's published_at (or first_seen_at — they
            should be close). For replayed triggers this is the
            historical published_at, NOT the current time.
        conn: optional connection. New one opened/closed if absent.
        assets, horizons_min: override the defaults above; mostly useful
            for tests that want a smaller cross product.

    Returns SchedulingResult with counts.
    """
    if trigger_fired_at.tzinfo is None:
        # We always store/compare in UTC. Reject naive datetimes
        # explicitly rather than silently assuming a timezone.
        raise ValueError(
            f"trigger_fired_at must be timezone-aware; got naive datetime "
            f"{trigger_fired_at!r}"
        )

    owned_conn = conn is None
    if owned_conn:
        conn = get_connection()

    rows_created = 0
    rows_skipped = 0

    try:
        cur = get_cursor(conn)
        for asset in assets:
            for horizon_min in horizons_min:
                target_at = trigger_fired_at + timedelta(minutes=horizon_min)
                cur.execute("""
                    INSERT INTO trigger_outcomes
                        (trigger_id, trigger_fired_at, asset,
                         horizon_minutes, target_at, status)
                    VALUES (%s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT (trigger_id, asset, horizon_minutes)
                    DO NOTHING
                    RETURNING id
                """, (trigger_id, trigger_fired_at, asset, horizon_min, target_at))
                if cur.fetchone() is not None:
                    rows_created += 1
                else:
                    rows_skipped += 1

        if owned_conn:
            conn.commit()

        return SchedulingResult(
            trigger_id=trigger_id,
            rows_created=rows_created,
            rows_skipped=rows_skipped,
        )
    finally:
        if owned_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _lookup_close_price(
    cur,
    asset: str,
    target: datetime,
    tolerance_min: int = PRICE_MATCH_TOLERANCE_MIN,
) -> Optional[float]:
    """Find the close price closest to `target` within ±tolerance_min.

    Returns None if no candle is within tolerance. We pick the closest
    candle (not the previous one) so 30-minute drift in either direction
    is symmetric. The 90-minute tolerance accommodates rare gaps in the
    candle history; in normal operation the closest candle is within
    30 minutes of any target.
    """
    cur.execute("""
        SELECT close, timestamp,
               EXTRACT(EPOCH FROM (timestamp - %s)) / 60 AS drift_min
        FROM price_snapshots
        WHERE symbol = %s
          AND timestamp BETWEEN %s - (%s * INTERVAL '1 minute')
                            AND %s + (%s * INTERVAL '1 minute')
        ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s))) ASC
        LIMIT 1
    """, (
        target, asset,
        target, tolerance_min,
        target, tolerance_min,
        target,
    ))
    row = cur.fetchone()
    return float(row["close"]) if row else None


def _resolve_one(cur, outcome: dict) -> tuple[str, dict]:
    """Resolve a single pending outcome row.

    Returns (new_status, fields_to_update). new_status is one of:
      'completed'    — both prices found, return computed
      'unavailable'  — confidently can't resolve; stop retrying
      'pending'      — defer; retry next pass

    The caller writes the update; this function pure-ish (only touches
    the DB to read prices). That makes it easier to test in isolation.

    Phase 6 detrending: when both fire and target prices are found,
    we additionally try to compute a baseline (the asset's return over
    the H-minute window immediately preceding fire time) and the excess
    return relative to that baseline. If the baseline price isn't
    available (e.g., fire happened too close to the start of price
    coverage), the outcome still completes — baseline_return_pct and
    excess_return_pct just stay null. Don't fail an otherwise-resolvable
    outcome over a missing baseline.
    """
    fired_price = _lookup_close_price(
        cur, outcome["asset"], outcome["trigger_fired_at"]
    )
    target_price = _lookup_close_price(
        cur, outcome["asset"], outcome["target_at"]
    )

    # Both missing → likely the price data hasn't been backfilled / the
    # event is too recent. Defer and retry, unless we've burned our
    # attempt budget.
    if fired_price is None and target_price is None:
        if outcome["attempts"] + 1 >= MAX_RESOLUTION_ATTEMPTS:
            return "unavailable", {
                "last_error": (
                    "no price candle within tolerance at fire or target "
                    "time; max attempts exhausted"
                ),
            }
        return "pending", {
            "last_error": "no price candle within tolerance (will retry)",
        }

    # Only one missing — could be transient (recent target) or permanent
    # (historical fire-time before backfill horizon). Treat the same:
    # defer until attempt budget is gone, then mark unavailable.
    if fired_price is None or target_price is None:
        which = "fire" if fired_price is None else "target"
        if outcome["attempts"] + 1 >= MAX_RESOLUTION_ATTEMPTS:
            return "unavailable", {
                "last_error": (
                    f"no price candle within tolerance at {which} time; "
                    f"max attempts exhausted"
                ),
            }
        return "pending", {
            "last_error": f"missing {which}-time price (will retry)",
        }

    # Both present — compute return.
    return_pct = (target_price - fired_price) / fired_price * 100.0

    # Detrending: look up the asset's price H minutes BEFORE fire time.
    # The window [fire - H, fire] is the prior period; its return is
    # what the market did in the absence of (this) trigger. Excess
    # return is trigger_return minus baseline. If the baseline price
    # is unavailable (e.g., fire is at the leading edge of our data),
    # the outcome still completes with baseline/excess null. Forecaster
    # naturally skips null cells via its existing null-filtering.
    from datetime import timedelta
    baseline_window_start = (
        outcome["trigger_fired_at"]
        - timedelta(minutes=outcome["horizon_minutes"])
    )
    baseline_start_price = _lookup_close_price(
        cur, outcome["asset"], baseline_window_start
    )
    if baseline_start_price is not None and baseline_start_price > 0:
        baseline_return_pct = (
            (fired_price - baseline_start_price) / baseline_start_price
            * 100.0
        )
        excess_return_pct = return_pct - baseline_return_pct
    else:
        baseline_return_pct = None
        excess_return_pct = None

    return "completed", {
        "return_pct": return_pct,
        "baseline_return_pct": baseline_return_pct,
        "excess_return_pct": excess_return_pct,
        "price_at_fire": fired_price,
        "price_at_target": target_price,
        "last_error": None,
    }


def resolve_pending(
    conn=None,
    batch_size: int = 200,
    now: Optional[datetime] = None,
) -> WorkerStats:
    """Worker pass: resolve all pending outcomes whose target_at has passed.

    Args:
        conn: optional connection. New one opened/closed if absent.
        batch_size: max outcomes to process in this pass. Tune up if the
            worker can't keep pace; tune down if it's slow on a single
            pass and you want finer-grained progress.
        now: override "current time" for testing replay scenarios.
            Defaults to datetime.now(UTC).

    Returns WorkerStats with counts.

    Idempotent and re-runnable. Safe to invoke from a cron every minute,
    or once per scheduled job, or from a replay script.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    owned_conn = conn is None
    if owned_conn:
        conn = get_connection()

    stats = WorkerStats(scanned=0, completed=0, unavailable=0,
                        deferred=0, errors=0)

    try:
        cur = get_cursor(conn)
        # Pull pending rows whose target time has passed. Order by
        # target_at so older rows resolve first (helpful for visibility
        # if there's a backlog). LIMIT bounds memory.
        cur.execute("""
            SELECT id, trigger_id, trigger_fired_at, asset, horizon_minutes,
                   target_at, attempts
            FROM trigger_outcomes
            WHERE status = 'pending'
              AND target_at <= %s
            ORDER BY target_at ASC
            LIMIT %s
        """, (now, batch_size))
        outcomes = cur.fetchall()
        stats.scanned = len(outcomes)

        for o in outcomes:
            try:
                new_status, fields = _resolve_one(cur, o)
            except Exception as e:
                # Unexpected error — log, increment attempts, defer.
                # Don't blow up the whole batch.
                logger.exception(
                    "Unexpected error resolving outcome %s: %s", o["id"], e
                )
                cur.execute("""
                    UPDATE trigger_outcomes
                    SET attempts = attempts + 1,
                        last_attempt_at = %s,
                        last_error = %s
                    WHERE id = %s
                """, (now, f"unexpected: {e}"[:500], o["id"]))
                stats.errors += 1
                continue

            # Build the UPDATE based on new_status. Always bump
            # attempts and last_attempt_at; conditionally set the
            # other fields.
            if new_status == "completed":
                cur.execute("""
                    UPDATE trigger_outcomes
                    SET status = 'completed',
                        return_pct = %s,
                        baseline_return_pct = %s,
                        excess_return_pct = %s,
                        price_at_fire = %s,
                        price_at_target = %s,
                        resolved_at = %s,
                        attempts = attempts + 1,
                        last_attempt_at = %s,
                        last_error = NULL
                    WHERE id = %s
                """, (
                    fields["return_pct"],
                    fields.get("baseline_return_pct"),
                    fields.get("excess_return_pct"),
                    fields["price_at_fire"],
                    fields["price_at_target"],
                    now, now, o["id"],
                ))
                stats.completed += 1
            elif new_status == "unavailable":
                cur.execute("""
                    UPDATE trigger_outcomes
                    SET status = 'unavailable',
                        attempts = attempts + 1,
                        last_attempt_at = %s,
                        last_error = %s
                    WHERE id = %s
                """, (now, fields.get("last_error"), o["id"]))
                stats.unavailable += 1
            else:  # pending — defer
                cur.execute("""
                    UPDATE trigger_outcomes
                    SET attempts = attempts + 1,
                        last_attempt_at = %s,
                        last_error = %s
                    WHERE id = %s
                """, (now, fields.get("last_error"), o["id"]))
                stats.deferred += 1

        if owned_conn:
            conn.commit()
        return stats
    finally:
        if owned_conn:
            conn.close()
