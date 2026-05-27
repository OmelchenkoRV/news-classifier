"""
Forecaster: topic-conditional prior distributions from trigger outcomes.

Given a trigger (signature, direction) and an asset/horizon pair,
returns the distribution of historical returns: mean, std, n, percentile
bands. This is the "topic-conditional base rate" the original plan
described.

Data sources:
  - public.trigger_outcomes  (live forward collection from Phase 5a)
  - replay.trigger_outcomes  (historical replay from Phase 5c)

The forecaster queries both, combines them in Python, and computes
statistics. Only outcomes with status='completed' contribute (pending
or unavailable rows are skipped — they're not yet evidence).

Two key design choices:

  - "Used at fire time" honesty. The priors_for_trigger() function takes
    a `before` timestamp. Only outcomes whose trigger_fired_at < before
    are included. When the alerter calls this at insert time, it passes
    the alert's own fire time — so the prior reflects only what was
    knowable at that moment. Without this, retrospective backtesting
    would silently leak future data into past alerts.

  - n-threshold (MIN_N=5). Cells with fewer than 5 outcomes return None
    rather than a noisy mean. This is the "don't pretend to know what
    you don't" constraint. Tune with care; lower N gives wider coverage
    but worse calibration.

Aggregation key:
    (signature_root, direction, asset, horizon_minutes)

where signature_root is the trigger signature with direction suffix
stripped. Triggers like 'iran-hormuz-escalation' and
'iran-hormuz-de-escalation' share root 'iran-hormuz' and aggregate
separately by direction. This matches Phase 2's directional split:
same topic, opposite directional priors.

Scope: this module is read-only. It does not write to any table. Use
calibrate_forecaster to evaluate, and the alerter to consume.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config.database import get_cursor

logger = logging.getLogger(__name__)


# Minimum number of outcomes required to surface a prior. Below this,
# the prior is statistical noise and we return None.
MIN_N: int = 5

# Default schemas to query. The forecaster checks each in turn and
# combines results. Order doesn't matter — we deduplicate by trigger_id
# (although same trigger_id in two schemas would be unexpected).
DEFAULT_SCHEMAS: tuple[str, ...] = ("public", "replay")


@dataclass
class PriorStats:
    """Statistics for a single (signature_root, direction, asset, horizon) cell."""
    n: int
    mean: float
    std: float
    p25: float
    p50: float
    p75: float

    def to_dict(self) -> dict:
        """JSON-friendly serialisation for storage on alert rows."""
        return {
            "n": self.n,
            "mean": round(self.mean, 4),
            "std": round(self.std, 4),
            "p25": round(self.p25, 4),
            "p50": round(self.p50, 4),
            "p75": round(self.p75, 4),
        }


def _strip_direction_suffix(signature: str) -> str:
    """Map a directional signature to its root.

      iran-hormuz-escalation     -> iran-hormuz
      iran-hormuz-de-escalation  -> iran-hormuz
      iran-hormuz                -> iran-hormuz   (no suffix to strip)

    The full set of suffixes lives in pipeline.triggers as
    _direction_suffix(); this function inverts it. We hand-list because
    the suffix mapping is small and stable; a regex would be marginally
    cleaner but more obscure.

    Suffix order matters: '-de-escalation' must be checked BEFORE
    '-escalation' because the former is a longer string that ends with
    the latter. Without this ordering, 'iran-hormuz-de-escalation'
    would match '-escalation' first and strip to 'iran-hormuz-de'.
    """
    for suffix in ("-de-escalation", "-escalation"):
        if signature.endswith(suffix):
            return signature[: -len(suffix)]
    return signature


def _fetch_outcomes_from_schema(
    conn, schema: str, before: Optional[datetime] = None,
) -> list[dict]:
    """Pull completed outcomes from one schema. Joins to triggers in the
    same schema for signature and direction. Direction-stripped root
    is computed in Python so we don't have to express the suffix logic
    in SQL twice (once here, once in pipeline.triggers).

    `before` filters by trigger_fired_at — only outcomes whose trigger
    fired strictly before this timestamp are returned. None = no filter.

    Returns a list of dicts:
      {trigger_id, signature, direction, asset, horizon_minutes,
       return_pct, trigger_fired_at}
    """
    cur = get_cursor(conn)
    # Quote schema name as identifier. We don't accept user-supplied
    # schemas through any UI, but defence-in-depth is cheap.
    from psycopg2 import sql, errors as psycopg2_errors
    from psycopg2.errors import UndefinedTable, InvalidSchemaName

    # Pre-check: if the schema doesn't exist, skip silently. This is
    # the only case we want to swallow — a fresh DB without replay
    # deployed shouldn't error the calibrator. Any OTHER failure
    # (permission denied, syntax error, missing column) should surface
    # so the operator can see what went wrong, rather than be hidden
    # behind a "no outcomes" message.
    cur.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    )
    if cur.fetchone() is None:
        logger.info("Schema %s not present; skipping.", schema)
        return []

    query = sql.SQL("""
        SELECT o.trigger_id, t.signature, t.direction, t.category,
               o.asset, o.horizon_minutes,
               o.return_pct, o.baseline_return_pct, o.excess_return_pct,
               o.trigger_fired_at
        FROM {schema}.trigger_outcomes o
        JOIN {schema}.triggers t ON t.id = o.trigger_id
        WHERE o.status = 'completed'
          {before_clause}
    """).format(
        schema=sql.Identifier(schema),
        before_clause=sql.SQL("AND o.trigger_fired_at < %s") if before else sql.SQL(""),
    )
    params = (before,) if before else ()
    try:
        cur.execute(query, params)
    except (UndefinedTable, InvalidSchemaName) as e:
        # Schema exists but the trigger_outcomes / triggers table is
        # missing. Treat as empty (this can happen mid-migration).
        logger.warning(
            "Schema %s exists but expected tables missing: %s",
            schema, e,
        )
        conn.rollback()
        return []
    except psycopg2_errors.UndefinedColumn:
        # Detrending columns aren't deployed yet. Fall back to the
        # legacy column set, synthesising None for the missing ones.
        # This keeps the forecaster working before the Phase 6 schema
        # migration is applied.
        logger.info(
            "Schema %s missing detrending columns; falling back to "
            "return_pct only.", schema,
        )
        conn.rollback()
        legacy_query = sql.SQL("""
            SELECT o.trigger_id, t.signature, t.direction, t.category,
                   o.asset, o.horizon_minutes, o.return_pct,
                   o.trigger_fired_at
            FROM {schema}.trigger_outcomes o
            JOIN {schema}.triggers t ON t.id = o.trigger_id
            WHERE o.status = 'completed'
              {before_clause}
        """).format(
            schema=sql.Identifier(schema),
            before_clause=sql.SQL("AND o.trigger_fired_at < %s") if before else sql.SQL(""),
        )
        cur.execute(legacy_query, params)
        rows = cur.fetchall()
        # Synthesise the missing columns as None so downstream code
        # doesn't have to special-case the legacy schema.
        for r in rows:
            r["baseline_return_pct"] = None
            r["excess_return_pct"] = None
        return list(rows)
    return list(cur.fetchall())


def fetch_all_outcomes(
    conn,
    schemas: tuple[str, ...] = DEFAULT_SCHEMAS,
    before: Optional[datetime] = None,
) -> list[dict]:
    """Pull completed outcomes from all configured schemas. Combined
    list — caller groups by aggregation key as needed."""
    rows: list[dict] = []
    for schema in schemas:
        rows.extend(_fetch_outcomes_from_schema(conn, schema, before=before))
    return rows


def _stats_from_returns(returns: list[float]) -> Optional[PriorStats]:
    """Compute summary statistics from a list of return percentages.
    Returns None if fewer than MIN_N values."""
    n = len(returns)
    if n < MIN_N:
        return None

    sorted_returns = sorted(returns)
    mean = statistics.mean(returns)
    # statistics.stdev requires n >= 2 (it's sample stdev). We've already
    # checked n >= MIN_N (=5), so this is safe.
    std = statistics.stdev(returns)

    # Percentiles via linear interpolation on the sorted list. Using
    # statistics.quantiles gives the same result with cleaner code.
    quartiles = statistics.quantiles(sorted_returns, n=4, method="inclusive")
    # quartiles is [p25, p50, p75]
    p25, p50, p75 = quartiles[0], quartiles[1], quartiles[2]

    return PriorStats(
        n=n, mean=mean, std=std,
        p25=p25, p50=p50, p75=p75,
    )


def _default_key_fn(row: dict) -> tuple:
    """Production aggregation key: (signature_root, direction, asset,
    horizon_minutes). Each cell answers 'what's the typical return for
    headlines on this specific topic in this direction?'

    This is the right key shape when there's enough recurrence per
    topic — i.e., when the same narrative thread fires multiple
    triggers over time. With thin data or unique-per-event
    signatures, cells end up at n=1 and most are dropped by MIN_N.
    """
    root = _strip_direction_suffix(row["signature"])
    return (root, row["direction"], row["asset"], row["horizon_minutes"])


def category_key_fn(row: dict) -> tuple:
    """Diagnostic aggregation key: (category, direction, asset,
    horizon_minutes). Each cell answers 'what's the typical return
    after, e.g., any geopolitical-escalation headline?'

    Loses topic specificity entirely — a Fed rate decision and an
    Iran missile launch end up in the same category bucket. But
    gives many more outcomes per cell, which makes the n threshold
    achievable on thin datasets. Useful as a diagnostic to answer
    'is there ANY signal at all?', not as a production prior.

    The row must include 'category'. Outcomes alone don't have it
    — fetch_all_outcomes joins to triggers but doesn't pull the
    category column. The calibrator's data path includes a separate
    fetch when this key is requested.
    """
    return (row["category"], row["direction"], row["asset"],
            row["horizon_minutes"])


def aggregate_priors(
    outcomes: list[dict],
    key_fn=_default_key_fn,
    metric: str = "return_pct",
) -> dict[tuple, PriorStats]:
    """Group outcomes by `key_fn(row)` and compute per-cell statistics.
    Cells with n < MIN_N are dropped.

    Default key_fn is (signature_root, direction, asset, horizon) —
    the production aggregation. Pass `category_key_fn` for the
    coarser diagnostic aggregation.

    Default metric is 'return_pct' (raw return). Pass 'excess_return_pct'
    for detrended priors — the excess of the trigger's actual return
    over the asset's prior-window return. Outcomes with NULL for the
    chosen metric are silently skipped (e.g., excess_return_pct may be
    NULL when the baseline price wasn't available at fire-H minutes).

    Returns a mapping from key tuple to PriorStats."""
    # Bucket the returns
    buckets: dict[tuple, list[float]] = {}
    for row in outcomes:
        value = row.get(metric)
        if value is None:
            continue
        key = key_fn(row)
        buckets.setdefault(key, []).append(float(value))

    # Compute stats per bucket, drop sparse cells
    out: dict[tuple, PriorStats] = {}
    for key, returns in buckets.items():
        stats = _stats_from_returns(returns)
        if stats is not None:
            out[key] = stats
    return out


def priors_for_trigger(
    conn,
    signature: str,
    direction: Optional[str],
    before: Optional[datetime] = None,
    schemas: tuple[str, ...] = DEFAULT_SCHEMAS,
    metric: str = "return_pct",
) -> dict:
    """Compute priors for a single trigger across all asset/horizon
    combinations. Output shape:

      {"BTCUSDT": {"60": {n: 14, mean: -0.42, std: 1.2, ...},
                   "240": {...},
                   "1440": {...}},
       "ETHUSDT": {...}}

    Cells with insufficient data are simply absent from the dict (not
    None). A trigger with no historical kin returns an empty dict {}.

    `metric` selects which return column to aggregate over. Default is
    'return_pct' (raw, current behavior). Pass 'excess_return_pct' for
    detrended priors. The alerter uses raw by default to preserve
    backwards compatibility; switching to detrended is a deliberate
    operational decision.

    Designed to be cheap enough to call at alert-insert time. With
    DEFAULT_SCHEMAS and a few thousand completed outcomes total, the
    query is fast (~50ms; indexed on trigger_id + completed). At the
    alert rate (single-digit per hour), one full-table-scan per alert
    is well below threshold.

    If outcome volume grows by orders of magnitude (10k+ outcomes) and
    alert rate also grows, the obvious optimisation is a periodically-
    refreshed materialised view of aggregated_priors keyed by
    (signature_root, direction, asset, horizon). Don't preempt that
    until the cost shows up in production.

    `before` enables honest backtesting / "as of fire time" priors.
    For live use at alert insertion: pass the alert's own creation
    timestamp (i.e. wall-clock NOW). For backtesting / replay alerts:
    pass the historical fire time.
    """
    root = _strip_direction_suffix(signature)
    rows = fetch_all_outcomes(conn, schemas=schemas, before=before)
    aggregated = aggregate_priors(rows, metric=metric)

    # Pull just the cells matching this trigger's (root, direction).
    # Returned shape is keyed by asset, then horizon (as string for
    # JSON-friendliness).
    out: dict = {}
    for (cell_root, cell_dir, asset, horizon), stats in aggregated.items():
        if cell_root != root:
            continue
        if cell_dir != direction:
            continue
        out.setdefault(asset, {})[str(horizon)] = stats.to_dict()
    return out
