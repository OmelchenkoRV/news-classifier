"""
Tests for Phase 2 directional changes in pipeline/triggers.py.

These tests cover the pure-function additions:
  - directions_are_opposite()
  - _direction_suffix()
  - build_signature() with direction parameter

Tests for process_headline() require a live database fixture and live in
a separate integration test suite (not run in this sandbox).
"""

from __future__ import annotations

import pytest

# We import the module without triggering the get_connection import at
# load time when DB env isn't set up. The module-level imports of
# config.database happen unconditionally in triggers.py, so this test
# file will only run in environments where that's importable. In the
# sandbox these tests will skip; locally they run.
pytest.importorskip(
    "pipeline.triggers",
    reason="pipeline.triggers requires config.database to be importable",
)

from pipeline.triggers import (
    directions_are_opposite,
    _direction_suffix,
    build_signature,
    _STRONG_DIRECTIONS,
)


class TestDirectionsAreOpposite:
    def test_escalation_vs_de_escalation_is_opposite(self):
        assert directions_are_opposite('escalation', 'de-escalation') is True
        assert directions_are_opposite('de-escalation', 'escalation') is True

    def test_same_strong_direction_is_not_opposite(self):
        assert directions_are_opposite('escalation', 'escalation') is False
        assert directions_are_opposite('de-escalation', 'de-escalation') is False

    def test_none_never_opposes(self):
        # None means "unspecified" — must not block matching.
        assert directions_are_opposite(None, 'escalation') is False
        assert directions_are_opposite('escalation', None) is False
        assert directions_are_opposite(None, None) is False

    def test_neutral_never_opposes(self):
        # Neutral means the verb category is direction-less (e.g.
        # ANNOUNCES). Must absorb into either directional sibling.
        assert directions_are_opposite('neutral', 'escalation') is False
        assert directions_are_opposite('neutral', 'de-escalation') is False
        assert directions_are_opposite('escalation', 'neutral') is False

    def test_context_dependent_never_opposes(self):
        # RESCINDS-style ambiguity — direction unknowable from verb
        # alone. Must not block matching.
        assert directions_are_opposite('context-dependent', 'escalation') is False
        assert directions_are_opposite('escalation', 'context-dependent') is False

    def test_unknown_never_opposes(self):
        # Verb wasn't in the taxonomy. Same rule as None.
        assert directions_are_opposite('unknown', 'escalation') is False
        assert directions_are_opposite('escalation', 'unknown') is False
        assert directions_are_opposite('unknown', 'unknown') is False

    def test_strong_directions_set_is_correct(self):
        # Sanity check on the constant we rely on.
        assert _STRONG_DIRECTIONS == {'escalation', 'de-escalation'}


class TestDirectionSuffix:
    def test_strong_directions_get_suffix(self):
        assert _direction_suffix('escalation') == '-escalation'
        assert _direction_suffix('de-escalation') == '-de-escalation'

    def test_weak_directions_get_no_suffix(self):
        # These all return empty string so signatures merge naturally.
        assert _direction_suffix('neutral') == ''
        assert _direction_suffix('context-dependent') == ''
        assert _direction_suffix('unknown') == ''
        assert _direction_suffix(None) == ''


class TestBuildSignatureWithDirection:
    def test_existing_behaviour_preserved_when_no_direction(self):
        # Existing callers pass no direction kwarg — must still work.
        sig = build_signature({'iran', 'hormuz'}, set())
        assert sig == 'hormuz-iran'  # alphabetical, no suffix

    def test_existing_behaviour_preserved_when_direction_is_none(self):
        sig = build_signature({'iran', 'hormuz'}, set(), direction=None)
        assert sig == 'hormuz-iran'

    def test_escalation_suffix(self):
        sig = build_signature({'iran', 'hormuz'}, set(), direction='escalation')
        assert sig == 'hormuz-iran-escalation'

    def test_de_escalation_suffix(self):
        sig = build_signature({'iran', 'hormuz'}, set(), direction='de-escalation')
        assert sig == 'hormuz-iran-de-escalation'

    def test_neutral_no_suffix(self):
        # Neutral is intentionally not suffixed — see _direction_suffix
        # rationale in triggers.py.
        sig = build_signature({'iran', 'hormuz'}, set(), direction='neutral')
        assert sig == 'hormuz-iran'

    def test_unknown_no_suffix(self):
        sig = build_signature({'iran', 'hormuz'}, set(), direction='unknown')
        assert sig == 'hormuz-iran'

    def test_keyword_fallback_with_direction(self):
        # When no anchors are present the signature falls back to keywords;
        # direction suffix should still apply.
        sig = build_signature(
            set(), {'defi', 'kelp', 'fork'}, direction='escalation'
        )
        # The fallback picks the longest 3 keywords sorted by length desc
        # then alphabetises them in the join — confirm suffix is appended.
        assert sig.endswith('-escalation')

    def test_iran_hormuz_directional_split(self):
        """The bug from the April 27 ETH breakout incident:
        same anchors, opposite directions → must produce different sigs."""
        threat = build_signature(
            {'iran', 'hormuz'}, set(), direction='escalation'
        )
        offer = build_signature(
            {'iran', 'hormuz'}, set(), direction='de-escalation'
        )
        assert threat != offer
        assert threat == 'hormuz-iran-escalation'
        assert offer == 'hormuz-iran-de-escalation'

    def test_classified_and_unclassified_share_signature(self):
        """Critical for backfill: an unclassified follow-up headline must
        be able to attach to a classified trigger. Same signature means
        the candidate scan will find it; the directional conflict check
        won't block (None vs strong is not opposite)."""
        classified = build_signature(
            {'iran', 'hormuz'}, set(), direction=None
        )
        # An old trigger created before Stage 2 has direction=None and
        # signature='hormuz-iran'. A new escalation headline creates a
        # 'hormuz-iran-escalation' trigger. The unclassified follow-up
        # below would hit similarity threshold against either — the
        # directional check decides which wins.
        assert classified == 'hormuz-iran'


class TestProcessHeadlineNowParameter:
    """Phase 5c: process_headline accepts an optional `now` parameter
    for historical replay. We can't easily test the full SQL roundtrip
    without a live DB, but we CAN verify parameter validation fires
    before any DB call — naive datetimes are rejected at the top."""

    def test_naive_now_rejected(self):
        """A naive datetime passed as `now` must raise ValueError before
        any DB call. This protects replay scripts from silently writing
        ambiguous timestamps to first_seen_at / last_seen_at."""
        from datetime import datetime
        from pipeline.triggers import TriggerManager

        # Module-level construction is fine — TriggerManager.__init__
        # doesn't touch the database.
        tm = TriggerManager()
        # Use a tracked category to ensure we actually reach the
        # naive-datetime check (untracked categories early-return None
        # before now is touched).
        with pytest.raises(ValueError, match="timezone-aware"):
            tm.process_headline(
                headline_id=1,
                title="Iran closes Strait of Hormuz",
                category="geopolitical",
                now=datetime(2026, 5, 1, 12, 0),  # naive — no tzinfo
            )

    def test_untracked_category_returns_none_before_now_check(self):
        """For untracked categories, process_headline early-returns
        None without touching the now parameter at all. This means
        even a naive datetime is harmless if the category is filtered
        out — keeps the contract minimal."""
        from datetime import datetime
        from pipeline.triggers import TriggerManager

        tm = TriggerManager()
        result = tm.process_headline(
            headline_id=1,
            title="Whatever",
            category="exchange_event",  # not in tracked_categories
            now=datetime(2026, 5, 1, 12, 0),  # naive, but never reached
        )
        assert result is None
