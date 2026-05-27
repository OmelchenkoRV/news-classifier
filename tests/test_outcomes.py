"""
Tests for pipeline.outcomes.

Exercises the scheduling and worker logic against a mocked DB cursor.
We don't run a real Postgres in tests — the cursor mock implements the
specific INSERT/SELECT/UPDATE shapes that outcomes.py issues, with
realistic semantics for the UNIQUE constraint, ORDER BY, and the
price-snapshot lookup.

What's tested:
  - schedule_outcomes creates the right cross product of asset×horizon
  - schedule_outcomes is idempotent (UNIQUE constraint honoured)
  - schedule_outcomes rejects naive datetimes
  - resolve_pending computes correct return percentages
  - resolve_pending handles missing prices (defer vs unavailable)
  - resolve_pending stops retrying after MAX_RESOLUTION_ATTEMPTS
  - resolve_pending matches prices within tolerance correctly
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from pipeline.outcomes import (
    schedule_outcomes, resolve_pending,
    TRACKED_ASSETS, TRACKED_HORIZONS_MIN,
    MAX_RESOLUTION_ATTEMPTS, PRICE_MATCH_TOLERANCE_MIN,
)


# ---------------------------------------------------------------------------
# Mock DB plumbing
#
# We need just enough fidelity to exercise outcomes.py's queries. The
# mock backs the trigger_outcomes table with a list of dicts and the
# price_snapshots table with another list. SQL is parsed by simple
# pattern-matching on the leading verb (INSERT/SELECT/UPDATE) and
# table name; we don't try to be a real SQL engine.
# ---------------------------------------------------------------------------
class _MockCursor:
    def __init__(self, store: dict):
        self.store = store
        self._last_result = None

    def execute(self, sql: str, params: tuple = ()):
        sql_lower = sql.lower().strip()
        if "insert into trigger_outcomes" in sql_lower:
            # Args order matches the INSERT in schedule_outcomes
            (trigger_id, trigger_fired_at, asset, horizon_min,
             target_at) = params[:5]
            key = (trigger_id, asset, horizon_min)
            if key in self.store["outcomes_by_key"]:
                # ON CONFLICT DO NOTHING — nothing to return
                self._last_result = None
                return
            row_id = len(self.store["outcomes"]) + 1
            row = {
                "id": row_id,
                "trigger_id": trigger_id,
                "trigger_fired_at": trigger_fired_at,
                "asset": asset,
                "horizon_minutes": horizon_min,
                "target_at": target_at,
                "status": "pending",
                "return_pct": None,
                "price_at_fire": None,
                "price_at_target": None,
                "resolved_at": None,
                "attempts": 0,
                "last_attempt_at": None,
                "last_error": None,
            }
            self.store["outcomes"].append(row)
            self.store["outcomes_by_key"][key] = row
            self._last_result = {"id": row_id}

        elif "from price_snapshots" in sql_lower:
            # _lookup_close_price query
            target, asset, _, tolerance_min, *_ = params
            tolerance = timedelta(minutes=tolerance_min)
            candidates = [
                p for p in self.store["prices"]
                if p["symbol"] == asset
                and abs((p["timestamp"] - target).total_seconds()) <= tolerance.total_seconds()
            ]
            if not candidates:
                self._last_result = None
                return
            # Pick the closest one (smallest absolute drift)
            best = min(
                candidates,
                key=lambda p: abs((p["timestamp"] - target).total_seconds()),
            )
            self._last_result = {
                "close": best["close"],
                "timestamp": best["timestamp"],
                "drift_min": (best["timestamp"] - target).total_seconds() / 60,
            }

        elif "from trigger_outcomes" in sql_lower and sql_lower.startswith("select"):
            # resolve_pending's SELECT
            now, batch_size = params
            pending = [
                o for o in self.store["outcomes"]
                if o["status"] == "pending" and o["target_at"] <= now
            ]
            pending.sort(key=lambda o: o["target_at"])
            self._last_result = pending[:batch_size]

        elif "update trigger_outcomes" in sql_lower:
            # We only need to handle the three update shapes from
            # resolve_pending. Match by which columns are being SET.
            row_id = params[-1]
            row = next(o for o in self.store["outcomes"] if o["id"] == row_id)
            if "set status = 'completed'" in sql_lower:
                # 8 params: return_pct, baseline_return_pct,
                # excess_return_pct, price_fire, price_target,
                # resolved_at, last_attempt, row_id
                (return_pct, baseline_return_pct, excess_return_pct,
                 price_fire, price_target,
                 resolved_at, last_attempt, _) = params
                row["status"] = "completed"
                row["return_pct"] = return_pct
                row["baseline_return_pct"] = baseline_return_pct
                row["excess_return_pct"] = excess_return_pct
                row["price_at_fire"] = price_fire
                row["price_at_target"] = price_target
                row["resolved_at"] = resolved_at
                row["last_attempt_at"] = last_attempt
                row["attempts"] += 1
                row["last_error"] = None
            elif "set status = 'unavailable'" in sql_lower:
                last_attempt, last_error, _ = params
                row["status"] = "unavailable"
                row["last_attempt_at"] = last_attempt
                row["attempts"] += 1
                row["last_error"] = last_error
            else:
                # plain "increment attempts and last_error" defer path
                last_attempt, last_error, _ = params
                row["last_attempt_at"] = last_attempt
                row["attempts"] += 1
                row["last_error"] = last_error
            self._last_result = None

    def fetchone(self):
        result = self._last_result
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def fetchall(self):
        result = self._last_result
        if isinstance(result, list):
            return result
        return [result] if result else []


class _MockConn:
    def __init__(self):
        self.store = {
            "outcomes": [],
            "outcomes_by_key": {},
            "prices": [],
        }
        self._cursor = _MockCursor(self.store)

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass

    # outcomes.py uses get_cursor(conn), which we shim through patching.


@pytest.fixture
def mock_conn(monkeypatch):
    conn = _MockConn()
    monkeypatch.setattr("pipeline.outcomes.get_cursor", lambda c: c._cursor)
    monkeypatch.setattr("pipeline.outcomes.get_connection", lambda: conn)
    return conn


def _add_price(conn, symbol: str, ts: datetime, close: float):
    """Helper to seed a price candle into the mock store."""
    conn.store["prices"].append({
        "symbol": symbol, "timestamp": ts, "close": close,
    })


# ---------------------------------------------------------------------------
# Scheduling tests
# ---------------------------------------------------------------------------
class TestScheduling:

    def test_creates_full_cross_product(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        result = schedule_outcomes(
            trigger_id=42, trigger_fired_at=fired_at, conn=mock_conn,
        )
        expected = len(TRACKED_ASSETS) * len(TRACKED_HORIZONS_MIN)
        assert result.rows_created == expected
        assert result.rows_skipped == 0
        assert len(mock_conn.store["outcomes"]) == expected
        # Each asset×horizon combination present exactly once
        keys = {
            (o["asset"], o["horizon_minutes"])
            for o in mock_conn.store["outcomes"]
        }
        assert keys == {
            (a, h) for a in TRACKED_ASSETS for h in TRACKED_HORIZONS_MIN
        }

    def test_target_at_computation(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        schedule_outcomes(
            trigger_id=42, trigger_fired_at=fired_at, conn=mock_conn,
        )
        for o in mock_conn.store["outcomes"]:
            expected_target = fired_at + timedelta(minutes=o["horizon_minutes"])
            assert o["target_at"] == expected_target

    def test_idempotent_rescheduling(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        first = schedule_outcomes(
            trigger_id=42, trigger_fired_at=fired_at, conn=mock_conn,
        )
        second = schedule_outcomes(
            trigger_id=42, trigger_fired_at=fired_at, conn=mock_conn,
        )
        assert first.rows_created > 0
        assert second.rows_created == 0
        assert second.rows_skipped == first.rows_created
        # No duplicates in the store
        expected = len(TRACKED_ASSETS) * len(TRACKED_HORIZONS_MIN)
        assert len(mock_conn.store["outcomes"]) == expected

    def test_naive_datetime_rejected(self, mock_conn):
        with pytest.raises(ValueError, match="timezone-aware"):
            schedule_outcomes(
                trigger_id=42,
                trigger_fired_at=datetime(2026, 5, 1, 12, 0),
                conn=mock_conn,
            )

    def test_custom_assets_and_horizons(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        result = schedule_outcomes(
            trigger_id=42, trigger_fired_at=fired_at, conn=mock_conn,
            assets=("BTCUSDT",),
            horizons_min=(60,),
        )
        assert result.rows_created == 1
        assert mock_conn.store["outcomes"][0]["asset"] == "BTCUSDT"
        assert mock_conn.store["outcomes"][0]["horizon_minutes"] == 60


# ---------------------------------------------------------------------------
# Resolution tests
# ---------------------------------------------------------------------------
class TestResolution:

    def _setup_trigger(self, conn, fired_at):
        """Schedule outcomes for a hypothetical trigger #1 with default
        cross product. Returns the scheduling result."""
        return schedule_outcomes(
            trigger_id=1, trigger_fired_at=fired_at, conn=conn,
            assets=("BTCUSDT",),
            horizons_min=(60,),  # single combo for focused tests
        )

    def test_completes_with_both_prices_present(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        target_at = fired_at + timedelta(minutes=60)
        self._setup_trigger(mock_conn, fired_at)
        # Seed exact-match prices
        _add_price(mock_conn, "BTCUSDT", fired_at, 80000.0)
        _add_price(mock_conn, "BTCUSDT", target_at, 81600.0)

        # Run worker with "now" past target time
        now = target_at + timedelta(minutes=5)
        stats = resolve_pending(conn=mock_conn, now=now)
        assert stats.completed == 1
        row = mock_conn.store["outcomes"][0]
        assert row["status"] == "completed"
        assert row["price_at_fire"] == 80000.0
        assert row["price_at_target"] == 81600.0
        # 80000 → 81600 = +2.0%
        assert row["return_pct"] == pytest.approx(2.0)

    def test_negative_return(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        target_at = fired_at + timedelta(minutes=60)
        self._setup_trigger(mock_conn, fired_at)
        _add_price(mock_conn, "BTCUSDT", fired_at, 80000.0)
        _add_price(mock_conn, "BTCUSDT", target_at, 79200.0)

        now = target_at + timedelta(minutes=5)
        resolve_pending(conn=mock_conn, now=now)
        row = mock_conn.store["outcomes"][0]
        # 80000 → 79200 = -1.0%
        assert row["return_pct"] == pytest.approx(-1.0)

    def test_target_in_future_not_resolved(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        target_at = fired_at + timedelta(minutes=60)
        self._setup_trigger(mock_conn, fired_at)
        _add_price(mock_conn, "BTCUSDT", fired_at, 80000.0)
        # Note: NO price at target; not yet observed
        # Worker time is BEFORE target_at
        now = fired_at + timedelta(minutes=30)
        stats = resolve_pending(conn=mock_conn, now=now)
        # Should not even scan it (target_at > now filter)
        assert stats.scanned == 0
        row = mock_conn.store["outcomes"][0]
        assert row["status"] == "pending"
        assert row["attempts"] == 0

    def test_missing_target_price_defers(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        # Use a 4-hour horizon so the tolerance window (90min) at the
        # fire timestamp can't reach the target timestamp. With a
        # 1-hour horizon and 90-min tolerance, a candle at fire-time
        # would also satisfy a target-time lookup, defeating the test.
        schedule_outcomes(
            trigger_id=1, trigger_fired_at=fired_at, conn=mock_conn,
            assets=("BTCUSDT",), horizons_min=(240,),
        )
        target_at = fired_at + timedelta(minutes=240)
        _add_price(mock_conn, "BTCUSDT", fired_at, 80000.0)
        # No target-time price; worker time past target
        now = target_at + timedelta(minutes=10)
        stats = resolve_pending(conn=mock_conn, now=now)
        assert stats.deferred == 1
        row = mock_conn.store["outcomes"][0]
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert "target" in (row["last_error"] or "").lower()

    def test_max_attempts_marks_unavailable(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        target_at = fired_at + timedelta(minutes=60)
        self._setup_trigger(mock_conn, fired_at)
        # No prices at all
        now = target_at + timedelta(minutes=10)
        for _ in range(MAX_RESOLUTION_ATTEMPTS):
            resolve_pending(conn=mock_conn, now=now)
        row = mock_conn.store["outcomes"][0]
        assert row["status"] == "unavailable"
        assert row["attempts"] == MAX_RESOLUTION_ATTEMPTS
        assert "max attempts exhausted" in (row["last_error"] or "")

    def test_price_within_tolerance_matches(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        target_at = fired_at + timedelta(minutes=60)
        self._setup_trigger(mock_conn, fired_at)
        # Hourly candle 30 minutes off-target
        _add_price(mock_conn, "BTCUSDT",
                   fired_at - timedelta(minutes=29), 80000.0)
        _add_price(mock_conn, "BTCUSDT",
                   target_at + timedelta(minutes=29), 81600.0)
        now = target_at + timedelta(minutes=60)
        stats = resolve_pending(conn=mock_conn, now=now)
        assert stats.completed == 1

    def test_price_outside_tolerance_defers(self, mock_conn):
        fired_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        target_at = fired_at + timedelta(minutes=60)
        self._setup_trigger(mock_conn, fired_at)
        # Both candles way outside tolerance
        far = timedelta(minutes=PRICE_MATCH_TOLERANCE_MIN + 30)
        _add_price(mock_conn, "BTCUSDT", fired_at - far, 80000.0)
        _add_price(mock_conn, "BTCUSDT", target_at + far, 81600.0)
        now = target_at + timedelta(hours=10)
        stats = resolve_pending(conn=mock_conn, now=now)
        # Both missing → defer (treated as transient on first attempt)
        assert stats.deferred == 1
        assert stats.completed == 0
