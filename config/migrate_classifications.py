"""
Migration: Add classifications table for live pipeline test mode.

Run once before starting the live pipeline:
    python -m config.migrate_classifications

This table stores the classifier's predictions separately from the
training labels, so test-mode predictions don't contaminate training data.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS classifications (
    id              SERIAL PRIMARY KEY,
    headline_id     INTEGER NOT NULL REFERENCES headlines(id),
    classified_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Model predictions
    impact_level            TEXT NOT NULL,
    impact_confidence       DOUBLE PRECISION NOT NULL,
    category                TEXT NOT NULL,
    category_confidence     DOUBLE PRECISION NOT NULL,
    is_causal               BOOLEAN NOT NULL,
    news_impact_score       DOUBLE PRECISION NOT NULL,
    should_tighten_gates    BOOLEAN NOT NULL,
    
    -- Metadata
    model_version   TEXT NOT NULL DEFAULT 'v2',
    processing_time_ms INTEGER,
    
    UNIQUE(headline_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_classifications_classified ON classifications(classified_at);
CREATE INDEX IF NOT EXISTS idx_classifications_causal ON classifications(is_causal);
CREATE INDEX IF NOT EXISTS idx_classifications_category ON classifications(category);
CREATE INDEX IF NOT EXISTS idx_classifications_tight ON classifications(should_tighten_gates);

-- View: recent high-confidence causal headlines
CREATE OR REPLACE VIEW v_recent_causal_alerts AS
SELECT 
    c.classified_at,
    h.published_at,
    h.source_name,
    h.title,
    c.category,
    c.category_confidence,
    c.impact_level,
    c.news_impact_score,
    c.should_tighten_gates
FROM classifications c
JOIN headlines h ON h.id = c.headline_id
WHERE c.is_causal = TRUE
  AND c.category_confidence > 0.6
ORDER BY c.classified_at DESC;

-- View: pipeline health metrics
CREATE OR REPLACE VIEW v_pipeline_health AS
SELECT 
    DATE_TRUNC('hour', classified_at) AS hour,
    COUNT(*) AS headlines_classified,
    COUNT(*) FILTER (WHERE is_causal) AS causal_count,
    COUNT(*) FILTER (WHERE should_tighten_gates) AS gate_tighten_count,
    AVG(processing_time_ms) AS avg_processing_ms,
    AVG(category_confidence) AS avg_cat_confidence
FROM classifications
GROUP BY DATE_TRUNC('hour', classified_at)
ORDER BY hour DESC;
"""


def migrate():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
        conn.commit()
        print("Migration complete: classifications table + views created")
    except Exception as e:
        print(f"Migration error: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
