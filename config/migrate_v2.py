"""
Database migration — add columns for v2 labeller.

Run this ONCE before using auto_label_v2.py.

Usage:
    python -m config.migrate_v2
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection

MIGRATION_SQL = """
-- Add new columns to headline_labels for v2 labeller
-- These are safe to run multiple times (IF NOT EXISTS / OR REPLACE)

ALTER TABLE headline_labels
    ADD COLUMN IF NOT EXISTS is_causal_capable BOOLEAN DEFAULT TRUE;

ALTER TABLE headline_labels
    ADD COLUMN IF NOT EXISTS pre_move_4h DOUBLE PRECISION;

ALTER TABLE headline_labels
    ADD COLUMN IF NOT EXISTS incremental_move_4h DOUBLE PRECISION;

-- Update impact_level to allow 'during_event' value
-- (no constraint change needed, it's just TEXT)

-- Add index for faster category queries
CREATE INDEX IF NOT EXISTS idx_labels_category ON headline_labels(category);
CREATE INDEX IF NOT EXISTS idx_labels_impact ON headline_labels(impact_level);
CREATE INDEX IF NOT EXISTS idx_labels_causal ON headline_labels(is_causal_capable);
"""


def migrate():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
        conn.commit()
        print("Migration complete: added is_causal_capable, pre_move_4h, incremental_move_4h columns")
    except Exception as e:
        print(f"Migration error: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
