"""
Migration: Event novelty trigger system.

Triggers represent market-moving narratives that decay over time.
A new narrative ("USA-Iran-war") moves markets on first exposure.
Every subsequent article about the same narrative has diminishing impact.

Run once before starting the trigger-aware pipeline:
    python -m config.migrate_triggers
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection

MIGRATION_SQL = """
-- ══════════════════════════════════════════════════════════════════
-- Triggers — unique event narratives with lifecycle state
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS triggers (
    id                  SERIAL PRIMARY KEY,
    signature           TEXT NOT NULL UNIQUE,    -- canonical key like "iran-hormuz-conflict"
    display_name        TEXT NOT NULL,           -- human-readable name
    category            TEXT NOT NULL,           -- geopolitical, regulatory, macro, etc
    
    -- Lifecycle timestamps
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- State machine: ACTIVE -> FADING -> STALE -> ARCHIVED
    state               TEXT NOT NULL DEFAULT 'ACTIVE',
    
    -- Current impact score (decays with age)
    impact_score        DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    
    -- How many headlines have mentioned this trigger
    mention_count       INTEGER NOT NULL DEFAULT 1,
    
    -- Keywords extracted for matching future headlines
    keywords            TEXT[] NOT NULL,
    
    -- Entities (people, places, orgs) extracted
    entities            TEXT[],
    
    -- Actor pairs (relational structure: 'iran+usa', 'fed+rate_cut')
    -- Same entities in different relationships get different pairs
    actor_pairs         TEXT[],
    
    -- Audit
    first_headline_id   INTEGER REFERENCES headlines(id),
    first_title         TEXT,
    
    CONSTRAINT valid_state CHECK (state IN ('ACTIVE', 'FADING', 'STALE', 'ARCHIVED'))
);

CREATE INDEX IF NOT EXISTS idx_triggers_state ON triggers(state);
CREATE INDEX IF NOT EXISTS idx_triggers_category ON triggers(category);
CREATE INDEX IF NOT EXISTS idx_triggers_last_seen ON triggers(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_triggers_keywords ON triggers USING GIN(keywords);

-- Add actor_pairs column if running against older schema
ALTER TABLE triggers ADD COLUMN IF NOT EXISTS actor_pairs TEXT[];
CREATE INDEX IF NOT EXISTS idx_triggers_pairs ON triggers USING GIN(actor_pairs);

-- ══════════════════════════════════════════════════════════════════
-- Trigger mentions — junction table linking headlines to triggers
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trigger_mentions (
    id              SERIAL PRIMARY KEY,
    trigger_id      INTEGER NOT NULL REFERENCES triggers(id),
    headline_id     INTEGER NOT NULL REFERENCES headlines(id),
    
    -- Was this the headline that caused the trigger state change?
    caused_creation BOOLEAN NOT NULL DEFAULT FALSE,
    caused_refresh  BOOLEAN NOT NULL DEFAULT FALSE,
    
    -- Similarity score at time of mention
    similarity_score DOUBLE PRECISION,
    
    mentioned_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(trigger_id, headline_id)
);

CREATE INDEX IF NOT EXISTS idx_mentions_trigger ON trigger_mentions(trigger_id);
CREATE INDEX IF NOT EXISTS idx_mentions_headline ON trigger_mentions(headline_id);

-- ══════════════════════════════════════════════════════════════════
-- Add trigger link to classifications
-- ══════════════════════════════════════════════════════════════════
ALTER TABLE classifications
    ADD COLUMN IF NOT EXISTS trigger_id INTEGER REFERENCES triggers(id),
    ADD COLUMN IF NOT EXISTS is_novel_event BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_classifications_trigger ON classifications(trigger_id);
CREATE INDEX IF NOT EXISTS idx_classifications_novel ON classifications(is_novel_event);

-- ══════════════════════════════════════════════════════════════════
-- Helpful views
-- ══════════════════════════════════════════════════════════════════
CREATE OR REPLACE VIEW v_active_triggers AS
SELECT 
    t.id, t.signature, t.display_name, t.category,
    t.state, t.impact_score, t.mention_count,
    t.first_seen_at, t.last_seen_at,
    NOW() - t.last_seen_at AS time_since_update,
    NOW() - t.first_seen_at AS age,
    t.keywords, t.first_title
FROM triggers t
WHERE t.state IN ('ACTIVE', 'FADING')
ORDER BY t.impact_score DESC, t.last_seen_at DESC;

CREATE OR REPLACE VIEW v_novel_events AS
SELECT
    c.classified_at,
    h.published_at,
    h.source_name,
    h.title,
    t.signature,
    t.display_name,
    t.state,
    t.impact_score,
    t.mention_count,
    c.category,
    c.should_tighten_gates
FROM classifications c
JOIN headlines h ON h.id = c.headline_id
LEFT JOIN triggers t ON t.id = c.trigger_id
WHERE c.is_novel_event = TRUE
ORDER BY c.classified_at DESC;
"""


def migrate():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
        conn.commit()
        print("Migration complete: triggers + trigger_mentions tables + views created")
        print("Added is_novel_event and trigger_id columns to classifications")
    except Exception as e:
        print(f"Migration error: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
