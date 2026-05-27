"""
Tests for pipeline.forecaster.

The forecaster's logic is mostly pure-function aggregation: take a list
of outcome dicts, group, compute statistics. Easy to test without a DB.
We test:

  - _strip_direction_suffix correctness
  - _stats_from_returns correctness on edge cases
  - aggregate_priors groups and filters correctly
  - n-threshold (MIN_N) drops sparse cells
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from pipeline.forecaster import (
    _strip_direction_suffix, _stats_from_returns,
    aggregate_priors, MIN_N, PriorStats,
)


def _outcome(signature: str, direction: str, asset: str,
             horizon: int, ret: float, fired_at: datetime = None) -> dict:
    """Helper to build an outcome dict mimicking what fetch_all_outcomes
    returns."""
    return {
        "trigger_id": 1,
        "signature": signature,
        "direction": direction,
        "asset": asset,
        "horizon_minutes": horizon,
        "return_pct": ret,
        "trigger_fired_at": fired_at or datetime(2026, 4, 20, tzinfo=timezone.utc),
    }


class TestStripDirectionSuffix:

    def test_escalation_stripped(self):
        assert _strip_direction_suffix("iran-hormuz-escalation") == "iran-hormuz"

    def test_de_escalation_stripped(self):
        assert _strip_direction_suffix("iran-hormuz-de-escalation") == "iran-hormuz"

    def test_no_suffix_unchanged(self):
        assert _strip_direction_suffix("iran-hormuz") == "iran-hormuz"

    def test_short_signature_unchanged(self):
        assert _strip_direction_suffix("iran") == "iran"

    def test_misleading_substring_not_stripped(self):
        # 'escalation' must be at the END for the strip to apply
        assert _strip_direction_suffix("escalation-policy") == "escalation-policy"


class TestStatsFromReturns:

    def test_returns_none_below_min_n(self):
        # MIN_N is 5; with 4 we should get None
        assert _stats_from_returns([1.0, 2.0, 3.0, 4.0]) is None
        assert _stats_from_returns([]) is None

    def test_basic_stats(self):
        # Exactly MIN_N returns
        returns = [1.0, 2.0, 3.0, 4.0, 5.0]
        s = _stats_from_returns(returns)
        assert s is not None
        assert s.n == 5
        assert s.mean == 3.0
        # statistics.stdev with these values: sqrt(sum((x-3)^2)/4) = sqrt(10/4) = sqrt(2.5)
        assert abs(s.std - 1.5811) < 0.001
        # p50 should be 3.0
        assert s.p50 == 3.0

    def test_negative_returns(self):
        returns = [-2.0, -1.5, -1.0, -0.5, 0.0]
        s = _stats_from_returns(returns)
        assert s is not None
        assert s.mean == -1.0
        assert s.p50 == -1.0

    def test_to_dict_serialisation(self):
        s = _stats_from_returns([1.0, 2.0, 3.0, 4.0, 5.0])
        d = s.to_dict()
        assert set(d.keys()) == {"n", "mean", "std", "p25", "p50", "p75"}
        assert d["n"] == 5
        # to_dict rounds to 4dp
        assert isinstance(d["mean"], float)


class TestAggregatePriors:

    def test_empty_input_returns_empty_dict(self):
        assert aggregate_priors([]) == {}

    def test_single_cell_above_threshold(self):
        # MIN_N outcomes for the same cell — should produce a prior
        outcomes = [
            _outcome("iran-hormuz-escalation", "escalation",
                     "BTCUSDT", 60, ret)
            for ret in [1.0, 2.0, 3.0, 4.0, 5.0]
        ]
        priors = aggregate_priors(outcomes)
        key = ("iran-hormuz", "escalation", "BTCUSDT", 60)
        assert key in priors
        assert priors[key].n == 5
        assert priors[key].mean == 3.0

    def test_below_threshold_dropped(self):
        # Only 4 outcomes — below MIN_N — should be excluded
        outcomes = [
            _outcome("iran-hormuz-escalation", "escalation",
                     "BTCUSDT", 60, ret)
            for ret in [1.0, 2.0, 3.0, 4.0]
        ]
        priors = aggregate_priors(outcomes)
        assert priors == {}

    def test_directional_split(self):
        """Triggers with the same root but different directions
        bucket separately."""
        outcomes = (
            [_outcome("iran-hormuz-escalation", "escalation",
                      "BTCUSDT", 60, -1.0) for _ in range(MIN_N)]
            + [_outcome("iran-hormuz-de-escalation", "de-escalation",
                        "BTCUSDT", 60, +1.0) for _ in range(MIN_N)]
        )
        priors = aggregate_priors(outcomes)
        esc = priors[("iran-hormuz", "escalation", "BTCUSDT", 60)]
        deesc = priors[("iran-hormuz", "de-escalation", "BTCUSDT", 60)]
        assert esc.mean == -1.0
        assert deesc.mean == +1.0

    def test_asset_horizon_split(self):
        """Same root + direction but different (asset, horizon) bucket
        separately."""
        outcomes = (
            [_outcome("iran-hormuz-escalation", "escalation",
                      "BTCUSDT", 60, 1.0) for _ in range(MIN_N)]
            + [_outcome("iran-hormuz-escalation", "escalation",
                        "BTCUSDT", 240, 2.0) for _ in range(MIN_N)]
            + [_outcome("iran-hormuz-escalation", "escalation",
                        "ETHUSDT", 60, 3.0) for _ in range(MIN_N)]
        )
        priors = aggregate_priors(outcomes)
        assert len(priors) == 3
        assert priors[("iran-hormuz", "escalation", "BTCUSDT", 60)].mean == 1.0
        assert priors[("iran-hormuz", "escalation", "BTCUSDT", 240)].mean == 2.0
        assert priors[("iran-hormuz", "escalation", "ETHUSDT", 60)].mean == 3.0

    def test_skips_null_returns(self):
        """Outcomes with return_pct=None are silently skipped."""
        outcomes = (
            [_outcome("iran-hormuz-escalation", "escalation",
                      "BTCUSDT", 60, ret)
             for ret in [1.0, 2.0, 3.0, 4.0, 5.0]]
            + [_outcome("iran-hormuz-escalation", "escalation",
                        "BTCUSDT", 60, None)]  # null — should be skipped
        )
        priors = aggregate_priors(outcomes)
        # Still 5 (the null was skipped, leaving exactly MIN_N)
        assert priors[("iran-hormuz", "escalation", "BTCUSDT", 60)].n == 5

    def test_unknown_direction_buckets_separately(self):
        """direction=None or 'unknown' are valid bucket keys.
        Triggers without direction shouldn't accidentally merge with
        directional ones."""
        outcomes = (
            [_outcome("iran-hormuz", None, "BTCUSDT", 60, 1.0)
             for _ in range(MIN_N)]
            + [_outcome("iran-hormuz", "escalation", "BTCUSDT", 60, 99.0)
               for _ in range(MIN_N)]
        )
        priors = aggregate_priors(outcomes)
        none_key = ("iran-hormuz", None, "BTCUSDT", 60)
        esc_key = ("iran-hormuz", "escalation", "BTCUSDT", 60)
        assert priors[none_key].mean == 1.0
        assert priors[esc_key].mean == 99.0


class TestAggregatePriorsMetric:
    """Phase 6: aggregate_priors accepts a `metric` parameter to choose
    between raw return and detrended (excess) return."""

    def _outcome_with_excess(self, ret_pct: float, excess_pct: float) -> dict:
        """Build an outcome dict with both return_pct and
        excess_return_pct populated. Same trigger_id/key for all so
        they bucket together."""
        return {
            "trigger_id": 1,
            "signature": "iran-hormuz-escalation",
            "direction": "escalation",
            "asset": "BTCUSDT",
            "horizon_minutes": 60,
            "return_pct": ret_pct,
            "baseline_return_pct": ret_pct - excess_pct,
            "excess_return_pct": excess_pct,
            "trigger_fired_at": datetime(2026, 4, 20, tzinfo=timezone.utc),
        }

    def test_default_uses_return_pct(self):
        """Default metric is return_pct — current behaviour unchanged."""
        outcomes = [self._outcome_with_excess(2.0, 0.5) for _ in range(5)]
        priors = aggregate_priors(outcomes)
        key = ("iran-hormuz", "escalation", "BTCUSDT", 60)
        assert priors[key].mean == 2.0   # raw, not excess

    def test_metric_excess_return_pct(self):
        """Pass metric='excess_return_pct' to aggregate over excess."""
        outcomes = [self._outcome_with_excess(2.0, 0.5) for _ in range(5)]
        priors = aggregate_priors(outcomes, metric="excess_return_pct")
        key = ("iran-hormuz", "escalation", "BTCUSDT", 60)
        assert priors[key].mean == 0.5   # excess, not raw

    def test_null_excess_filtered(self):
        """Outcomes with NULL excess_return_pct are skipped under
        metric='excess_return_pct', even if their return_pct is set.
        This handles the partially-backfilled state where some old
        outcomes never got a baseline computed."""
        outcomes = [
            self._outcome_with_excess(1.0, 0.5) for _ in range(5)
        ]
        # Add one with valid return but no excess (couldn't compute
        # baseline at fire time)
        partial = self._outcome_with_excess(99.0, 0.5)
        partial["excess_return_pct"] = None
        partial["baseline_return_pct"] = None
        outcomes.append(partial)
        # Aggregating by excess: only the 5 valid ones count.
        priors_excess = aggregate_priors(outcomes, metric="excess_return_pct")
        key = ("iran-hormuz", "escalation", "BTCUSDT", 60)
        assert priors_excess[key].n == 5
        assert priors_excess[key].mean == 0.5
        # Aggregating by return_pct: all 6 count (including the partial).
        priors_raw = aggregate_priors(outcomes, metric="return_pct")
        assert priors_raw[key].n == 6
