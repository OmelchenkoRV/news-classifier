"""
Schema migration for the COVID BDBA research exercise.

Adds tables for historical news + market data + BDBA topic tracking.
These are SEPARATE from the live tables — research data shouldn't
contaminate production analytics.

Run once:
    python -m research.covid_exercise.schema
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from config.database import get_connection, get_cursor


SCHEMA = """
-- ════════════════════════════════════════════════════════════════
-- HISTORICAL NEWS DATA (GDELT pulls + supplementary sources)
-- ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS historical_headlines (
    id              SERIAL PRIMARY KEY,
    -- Provenance
    source_type     TEXT NOT NULL,        -- 'gdelt', 'wayback', 'manual'
    source_domain   TEXT,                  -- e.g. 'reuters.com'
    source_country  TEXT,                  -- ISO country code if known
    
    -- Content
    title           TEXT NOT NULL,
    url             TEXT,
    published_at    TIMESTAMPTZ NOT NULL,
    
    -- Categorization for analysis
    research_topic  TEXT NOT NULL,        -- 'covid', 'ebola_2014', 'mers_2015', 'uap_2017'
    keyword_matches TEXT[],                -- which keywords matched
    
    -- Tier classification (helps with source diversification analysis)
    source_tier     TEXT,                  -- 'tier1' (Reuters, WSJ, NYT, Bloomberg)
                                           -- 'tier2' (regional majors, Guardian, etc)
                                           -- 'tier3' (specialty, blogs, tabloids)
    
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(url, published_at)
);

CREATE INDEX IF NOT EXISTS idx_hh_topic_date
    ON historical_headlines(research_topic, published_at);
CREATE INDEX IF NOT EXISTS idx_hh_domain_date
    ON historical_headlines(source_domain, published_at);


-- ════════════════════════════════════════════════════════════════
-- HISTORICAL MARKET DATA (Yahoo + Binance daily)
-- ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS historical_market_daily (
    id              SERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,         -- 'SPY', 'VIX', 'BTC-USD', 'CL=F' (oil)
    trade_date      DATE NOT NULL,
    open_price      DOUBLE PRECISION,
    high_price      DOUBLE PRECISION,
    low_price       DOUBLE PRECISION,
    close_price     DOUBLE PRECISION,
    adj_close       DOUBLE PRECISION,
    volume          BIGINT,
    daily_return    DOUBLE PRECISION,      -- pre-computed: (close - prev_close) / prev_close
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ticker, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_hmd_ticker_date
    ON historical_market_daily(ticker, trade_date);


-- ════════════════════════════════════════════════════════════════
-- BDBA TOPIC DEFINITIONS
-- ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS bdba_topics (
    name            TEXT PRIMARY KEY,      -- 'pandemic_signal', 'ufo_uap', etc
    description     TEXT,
    
    -- Keyword matching config
    primary_keywords TEXT[] NOT NULL,      -- if any present, headline matches
    boost_keywords  TEXT[],                -- if present, increases match weight
    exclude_keywords TEXT[],               -- if present, excludes (e.g. 'flu shot' to exclude routine flu)
    
    -- Configuration
    baseline_window_days INTEGER DEFAULT 30,
    alert_z_threshold DOUBLE PRECISION DEFAULT 3.0,
    min_baseline_count DOUBLE PRECISION DEFAULT 2.0,  -- below this, baseline considered noisy
    
    -- Metadata
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    notes           TEXT
);


-- ════════════════════════════════════════════════════════════════
-- RETROSPECTIVE SURVEILLANCE OUTPUT
-- ════════════════════════════════════════════════════════════════
-- Stores what alerts WOULD have fired if the BDBA system was running

CREATE TABLE IF NOT EXISTS bdba_retrospective (
    id              SERIAL PRIMARY KEY,
    research_topic  TEXT NOT NULL,         -- which historical event
    bdba_topic      TEXT NOT NULL,         -- which BDBA topic matched
    
    -- The day this snapshot represents
    snapshot_date   DATE NOT NULL,
    
    -- Statistics computed on this day's data
    headlines_24h   INTEGER NOT NULL,
    headlines_7d    INTEGER NOT NULL,
    baseline_30d    DOUBLE PRECISION,      -- mean over previous 30 days
    baseline_std    DOUBLE PRECISION,
    z_score         DOUBLE PRECISION,      -- (current - baseline) / std
    
    -- Source diversification
    unique_domains_24h INTEGER,
    tier1_count_24h INTEGER,                -- mainstream coverage indicator
    tier1_count_baseline DOUBLE PRECISION,
    
    -- Alert decision
    would_alert     BOOLEAN NOT NULL,
    alert_severity  TEXT,                   -- 'watch', 'warm', 'hot'
    alert_reasons   TEXT[],                 -- ['frequency', 'tier1_diversification']
    
    -- For correlating with markets
    spy_close       DOUBLE PRECISION,
    spy_return_5d_fwd DOUBLE PRECISION,    -- 5-day forward return after this date
    spy_return_30d_fwd DOUBLE PRECISION,
    vix_close       DOUBLE PRECISION,
    btc_close       DOUBLE PRECISION,
    btc_return_5d_fwd DOUBLE PRECISION,
    
    UNIQUE(research_topic, bdba_topic, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_bdba_retro_topic_date
    ON bdba_retrospective(research_topic, snapshot_date);


-- ════════════════════════════════════════════════════════════════
-- USEFUL VIEWS FOR ANALYSIS
-- ════════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW v_covid_timeline AS
SELECT
    snapshot_date,
    headlines_24h,
    headlines_7d,
    z_score,
    unique_domains_24h,
    tier1_count_24h,
    would_alert,
    alert_severity,
    spy_close,
    spy_return_5d_fwd,
    vix_close,
    btc_close
FROM bdba_retrospective
WHERE research_topic = 'covid'
  AND bdba_topic = 'pandemic_signal'
ORDER BY snapshot_date;

CREATE OR REPLACE VIEW v_alert_lead_times AS
SELECT
    research_topic,
    bdba_topic,
    -- First alert day
    MIN(snapshot_date) FILTER (WHERE would_alert) AS first_alert_date,
    -- First major market move (>2% drop)
    MIN(snapshot_date) FILTER (WHERE spy_return_5d_fwd < -0.02) AS first_major_drop,
    -- Lead time in days
    MIN(snapshot_date) FILTER (WHERE spy_return_5d_fwd < -0.02) -
    MIN(snapshot_date) FILTER (WHERE would_alert) AS lead_time_days,
    COUNT(*) FILTER (WHERE would_alert) AS total_alerts,
    COUNT(*) AS total_days
FROM bdba_retrospective
GROUP BY research_topic, bdba_topic;
"""


def main():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute(SCHEMA)
    conn.commit()
    conn.close()
    print("COVID exercise tables created.")
    print()
    print("Tables added:")
    print("  - historical_headlines")
    print("  - historical_market_daily")
    print("  - bdba_topics")
    print("  - bdba_retrospective")
    print()
    print("Views added:")
    print("  - v_covid_timeline")
    print("  - v_alert_lead_times")


if __name__ == "__main__":
    main()
