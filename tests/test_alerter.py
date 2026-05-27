"""
Tests for pipeline.alerter.

Same mocked-DB pattern as test_outcomes. Exercises insert_alert with
full args, minimal args, duplicate suppression, and JSON serialisation
of the priors field.
"""

from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone

from pipeline.alerter import insert_alert, AlertInsertResult


class _MockCursor:
    """Tiny mock that handles the single INSERT shape from insert_alert."""

    def __init__(self, store: dict):
        self.store = store
        self._last_result = None

    def execute(self, sql: str, params: tuple = ()):
        sql_lower = sql.lower().strip()
        if "insert into alerts" in sql_lower:
            (headline_id, trigger_id,
             headline_title, headline_published_at,
             trigger_signature, trigger_display_name,
             category,
             direction, verb_category,
             is_novel, is_direction_change,
             impact_score, mention_count,
             priors_json) = params

            # Honour ON CONFLICT DO NOTHING on (headline_id)
            if headline_id in self.store["by_headline_id"]:
                self._last_result = None
                return

            row_id = len(self.store["alerts"]) + 1
            row = {
                "id": row_id,
                "headline_id": headline_id,
                "trigger_id": trigger_id,
                "headline_title": headline_title,
                "headline_published_at": headline_published_at,
                "trigger_signature": trigger_signature,
                "trigger_display_name": trigger_display_name,
                "category": category,
                "direction": direction,
                "verb_category": verb_category,
                "is_novel": is_novel,
                "is_direction_change": is_direction_change,
                "impact_score": impact_score,
                "mention_count": mention_count,
                "priors_json": priors_json,  # raw json string from insert_alert
                "consumed_at": None,
                "acked_by": None,
            }
            self.store["alerts"].append(row)
            self.store["by_headline_id"][headline_id] = row
            self._last_result = {"id": row_id}

    def fetchone(self):
        return self._last_result


class _MockConn:
    def __init__(self):
        self.store = {"alerts": [], "by_headline_id": {}}
        self._cursor = _MockCursor(self.store)


@pytest.fixture
def mock_conn(monkeypatch):
    conn = _MockConn()
    monkeypatch.setattr("pipeline.alerter.get_cursor", lambda c: c._cursor)
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestInsertAlert:

    def test_minimal_args(self, mock_conn):
        """Bare minimum required arguments — most fields nullable."""
        result = insert_alert(
            mock_conn,
            headline_id=1,
            headline_title="Iran closes Strait of Hormuz",
            headline_published_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            category="geopolitical",
            is_novel=True,
        )
        assert result.alert_id == 1
        assert not result.was_duplicate
        row = mock_conn.store["alerts"][0]
        assert row["headline_id"] == 1
        assert row["trigger_id"] is None
        assert row["direction"] is None
        assert row["priors_json"] is None
        assert row["is_direction_change"] is False  # default

    def test_full_args(self, mock_conn):
        """Full population — all args from a real trigger-attached alert."""
        priors = {
            "BTCUSDT": {"60": {"mean": -0.4, "n": 14, "std": 1.2}},
        }
        result = insert_alert(
            mock_conn,
            headline_id=42,
            headline_title="Iran closes Strait of Hormuz",
            headline_published_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            category="geopolitical",
            is_novel=True,
            is_direction_change=False,
            trigger_id=7,
            trigger_signature="hormuz-iran-escalation",
            trigger_display_name="Iran-Hormuz escalation",
            direction="escalation",
            verb_category="CLOSES",
            impact_score=1.0,
            mention_count=1,
            priors=priors,
        )
        assert result.alert_id == 1
        assert not result.was_duplicate
        row = mock_conn.store["alerts"][0]
        assert row["trigger_id"] == 7
        assert row["trigger_signature"] == "hormuz-iran-escalation"
        assert row["direction"] == "escalation"
        assert row["verb_category"] == "CLOSES"
        # priors should be JSON-encoded
        assert json.loads(row["priors_json"]) == priors

    def test_duplicate_suppressed(self, mock_conn):
        """Same headline → second insert is no-op."""
        first = insert_alert(
            mock_conn,
            headline_id=1,
            headline_title="X",
            headline_published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            category="geopolitical",
            is_novel=True,
        )
        second = insert_alert(
            mock_conn,
            headline_id=1,  # same headline
            headline_title="X (re-inserted)",
            headline_published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            category="geopolitical",
            is_novel=True,
        )
        assert first.alert_id == 1
        assert second.alert_id is None
        assert second.was_duplicate is True
        # Only one row stored
        assert len(mock_conn.store["alerts"]) == 1

    def test_no_priors_serialises_to_null(self, mock_conn):
        """priors=None → priors_json=None (not 'null' string)."""
        insert_alert(
            mock_conn,
            headline_id=1,
            headline_title="X",
            headline_published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            category="geopolitical",
            is_novel=True,
            priors=None,
        )
        row = mock_conn.store["alerts"][0]
        assert row["priors_json"] is None  # not "null" or "{}"

    def test_empty_priors_serialises_to_empty_object(self, mock_conn):
        """priors={} should serialise to '{}', not be treated as None."""
        insert_alert(
            mock_conn,
            headline_id=1,
            headline_title="X",
            headline_published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            category="geopolitical",
            is_novel=True,
            priors={},
        )
        row = mock_conn.store["alerts"][0]
        assert row["priors_json"] == "{}"
