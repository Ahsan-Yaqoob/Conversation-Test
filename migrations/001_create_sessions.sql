-- Run this once in the Supabase SQL Editor:
-- https://supabase.com/dashboard/project/dqjtorcujhauozenfvch/sql/new

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    status           TEXT,
    services         JSONB,
    msg_count        INTEGER,
    scraped_at       TEXT,
    conversation     JSONB,
    result_json      JSONB,
    reference_data   JSONB,
    analysis_status  TEXT,
    analysis_summary TEXT,
    analysis_issues  JSONB,
    extractor_rating INTEGER,
    rating_reason    TEXT,
    analyzed_at      TEXT,
    db_updated_at    TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Fast lookup indexes
CREATE INDEX IF NOT EXISTS idx_sessions_scraped_at       ON sessions (scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_analysis_status  ON sessions (analysis_status);
CREATE INDEX IF NOT EXISTS idx_sessions_extractor_rating ON sessions (extractor_rating);
