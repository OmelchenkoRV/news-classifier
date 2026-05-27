"""
Alerter: insert a row into the `alerts` table.

Single-purpose module. The live pipeline calls `insert_alert` whenever
its `effective_tighten` decision fires; this writes a denormalised row
to the alerts table, which a Postgres trigger broadcasts on the
`alerts_new` NOTIFY channel.

Why a separate module rather than inline SQL in live.py? Two reasons:

  1. The alert payload is the API contract with crypto-yield-management-
     system. Centralising it here means the schema's denormalisation
     decisions (what to copy, what to leave as a reference) live in one
     place and are obvious to read. If we later want to add a column to
     alerts, this is the only application code that needs updating.

  2. Testability. A small function with a small surface area is easier
     to unit-test than a path through live.py's main loop.

The module is deliberately thin: no batching, no retries, no caching.
If the insert fails (unique constraint, FK violation, db down), the
exception propagates back to the caller, which logs and continues —
losing the alert is preferable to crashing the live pipeline. Phase 5d
will add prior data to the insert; the function signature already
accepts a `priors` dict for forward compatibility.

NOTIFY happens automatically via Postgres trigger
(notify_alert_inserted). Application code does not invoke pg_notify
directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from config.database import get_cursor

logger = logging.getLogger(__name__)


@dataclass
class AlertInsertResult:
    """What insert_alert returns. `alert_id` is None when the insert was
    suppressed by the unique constraint (already exists for this
    headline) — distinguishing this from a successful insert lets the
    caller log appropriately."""
    alert_id: Optional[int]
    was_duplicate: bool


def insert_alert(
    conn,
    *,
    headline_id: int,
    headline_title: str,
    headline_published_at,  # datetime, may be None for legacy rows
    category: str,
    is_novel: bool,
    is_direction_change: bool = False,
    trigger_id: Optional[int] = None,
    trigger_signature: Optional[str] = None,
    trigger_display_name: Optional[str] = None,
    direction: Optional[str] = None,
    verb_category: Optional[str] = None,
    impact_score: Optional[float] = None,
    mention_count: Optional[int] = None,
    priors: Optional[dict] = None,
) -> AlertInsertResult:
    """Insert a single alert row.

    Most arguments are keyword-only because we have a lot of them and
    positional would be a maintenance hazard. The only positional arg
    is `conn`, since the live pipeline passes the same connection it's
    using for the rest of the per-headline work.

    The unique constraint on `headline_id` means re-running this for
    the same headline is a no-op (returns AlertInsertResult with
    alert_id=None, was_duplicate=True). The caller decides whether
    that's worth logging.

    Args:
        conn: open DB connection. Caller manages commit/rollback.
        headline_id: id of the headline that triggered this alert.
        headline_title: the headline text (denormalised for consumer).
        headline_published_at: when the news was published (denormalised).
        category: classifier category (e.g. 'geopolitical').
        is_novel: True iff this headline created a brand-new trigger.
        is_direction_change: True iff this mention's direction differs
            from the trigger's recorded direction in a strong way.
        trigger_id: trigger this alert is attached to. May be None
            in the rare case where should_tighten_gates fired but no
            trigger could be formed.
        trigger_signature, trigger_display_name: snapshotted from the
            triggers table at fire time.
        direction, verb_category: Stage 2 outputs at fire time.
        impact_score, mention_count: trigger state at fire time.
        priors: forecaster-computed prior distributions, populated by
            Phase 5d. Stored as JSONB. Passed as a dict; serialised
            to JSON here.

    Returns AlertInsertResult.
    """
    cur = get_cursor(conn)
    priors_json = json.dumps(priors) if priors is not None else None
    cur.execute("""
        INSERT INTO alerts (
            headline_id, trigger_id,
            headline_title, headline_published_at,
            trigger_signature, trigger_display_name,
            category,
            direction, verb_category,
            is_novel, is_direction_change,
            impact_score, mention_count,
            priors
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
        )
        ON CONFLICT (headline_id) DO NOTHING
        RETURNING id
    """, (
        headline_id, trigger_id,
        headline_title, headline_published_at,
        trigger_signature, trigger_display_name,
        category,
        direction, verb_category,
        is_novel, is_direction_change,
        impact_score, mention_count,
        priors_json,
    ))
    row = cur.fetchone()
    if row is None:
        # ON CONFLICT suppressed the insert
        return AlertInsertResult(alert_id=None, was_duplicate=True)
    return AlertInsertResult(alert_id=row["id"], was_duplicate=False)
