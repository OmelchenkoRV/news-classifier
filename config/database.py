"""
Database configuration and schema creation.

Reads connection details from .env file:
  DB_HOST=localhost
  DB_PORT=5432
  DB_NAME=news_classifier
  DB_USER=postgres
  DB_PASSWORD=your_password
  COINDESK_API_KEY=your_key
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    """Get a PostgreSQL connection using .env configuration."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "news_classifier"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def get_cursor(conn):
    """Get a dict cursor for easier result handling."""
    return conn.cursor(cursor_factory=RealDictCursor)


SCHEMA_SQL = """
-- ══════════════════════════════════════════════════════════════════
-- RSS feed sources — the curated list of news feeds to poll
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS rss_sources (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,               -- human-readable name, e.g. "CoinDesk"
    url             TEXT NOT NULL UNIQUE,         -- RSS/Atom feed URL
    category        TEXT NOT NULL DEFAULT 'general',  -- general, defi, bitcoin, ethereum, regulation, macro
    tier            INTEGER NOT NULL DEFAULT 2,  -- 1=major (CoinDesk, CoinTelegraph), 2=mid, 3=niche
    language        TEXT NOT NULL DEFAULT 'en',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_fetched_at TIMESTAMPTZ,
    last_error      TEXT,
    error_count     INTEGER NOT NULL DEFAULT 0,  -- consecutive errors; disable after 10
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ══════════════════════════════════════════════════════════════════
-- Headlines — raw collected headlines from all sources
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS headlines (
    id              SERIAL PRIMARY KEY,
    source_name     TEXT NOT NULL,                -- e.g. "coindesk", "cointelegraph"
    source_type     TEXT NOT NULL DEFAULT 'rss',  -- 'rss', 'api_coindesk', 'api_other'
    title           TEXT NOT NULL,
    url             TEXT,
    body_snippet    TEXT,                         -- first 500 chars of article body if available
    published_at    TIMESTAMPTZ NOT NULL,
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tickers         TEXT[],                       -- mentioned tickers: {'BTC', 'ETH', ...}

    -- Deduplication
    title_hash      TEXT NOT NULL,                -- SHA256 of lowercase stripped title
    UNIQUE(title_hash)
);

CREATE INDEX IF NOT EXISTS idx_headlines_published ON headlines(published_at);
CREATE INDEX IF NOT EXISTS idx_headlines_source ON headlines(source_name);

-- ══════════════════════════════════════════════════════════════════
-- Price snapshots — for cross-referencing headlines with price moves
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS price_snapshots (
    id              SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,                -- 'BTCUSDT', 'ETHUSDT'
    timestamp       TIMESTAMPTZ NOT NULL,
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION NOT NULL,
    volume          DOUBLE PRECISION,
    UNIQUE(symbol, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_prices_symbol_ts ON price_snapshots(symbol, timestamp);

-- ══════════════════════════════════════════════════════════════════
-- Labels — auto-generated and manually reviewed impact labels
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS headline_labels (
    id              SERIAL PRIMARY KEY,
    headline_id     INTEGER NOT NULL REFERENCES headlines(id),

    -- Auto-labelled by price impact
    price_move_1h   DOUBLE PRECISION,            -- max absolute % move in 1h after headline
    price_move_4h   DOUBLE PRECISION,
    price_move_24h  DOUBLE PRECISION,
    price_move_48h  DOUBLE PRECISION,

    -- Auto-assigned impact level
    impact_level    TEXT,                         -- 'high', 'medium', 'low', 'noise'
    impact_direction TEXT,                        -- 'bullish', 'bearish', 'neutral'

    -- Manual review (filled later for high-impact headlines)
    category        TEXT,                         -- 'geopolitical', 'regulatory', 'exchange',
                                                 -- 'macro', 'protocol', 'market_structure', 'noise'
    manually_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_at     TIMESTAMPTZ,

    -- Model predictions (filled after training)
    predicted_category   TEXT,
    predicted_impact     TEXT,
    prediction_confidence DOUBLE PRECISION,

    UNIQUE(headline_id)
);

-- ══════════════════════════════════════════════════════════════════
-- Collection runs — track backfill and live collection progress
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS collection_runs (
    id              SERIAL PRIMARY KEY,
    source_type     TEXT NOT NULL,                -- 'coindesk_api', 'rss', 'binance_prices'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    records_fetched INTEGER DEFAULT 0,
    records_new     INTEGER DEFAULT 0,            -- new (non-duplicate) records
    api_calls_used  INTEGER DEFAULT 0,            -- for CoinDesk budget tracking
    status          TEXT DEFAULT 'running',       -- 'running', 'completed', 'failed'
    error_message   TEXT,
    metadata        JSONB                         -- any extra info
);
"""


def create_schema():
    """Create all tables if they don't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
        print("Schema created successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    create_schema()
