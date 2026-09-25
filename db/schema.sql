-- Real-Time NFL & NBA Injury Alert Service Database Schema
-- Supabase / PostgreSQL Free Tier

-- Table: alerts
CREATE TABLE IF NOT EXISTS alerts (
  id BIGSERIAL PRIMARY KEY,
  sport VARCHAR(10) NOT NULL,
  team VARCHAR(10) NOT NULL,
  player_name VARCHAR(100) NOT NULL,
  status VARCHAR(50) NOT NULL,
  source VARCHAR(50) NOT NULL,
  timestamp_source TIMESTAMPTZ NOT NULL,
  timestamp_first_seen TIMESTAMPTZ NOT NULL,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  verbatim_text TEXT NOT NULL,
  source_url TEXT NOT NULL,
  verified BOOLEAN NOT NULL DEFAULT FALSE,
  game_id VARCHAR(50),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for performance (<60ms query latency)
CREATE INDEX IF NOT EXISTS idx_alerts_sport_team ON alerts(sport, team);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_timestamp_first_seen ON alerts(timestamp_first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_game_id ON alerts(game_id);

-- Table: games
CREATE TABLE IF NOT EXISTS games (
  id BIGSERIAL PRIMARY KEY,
  sport VARCHAR(10) NOT NULL,
  game_id VARCHAR(50) NOT NULL UNIQUE,
  club_1 VARCHAR(10) NOT NULL,
  club_2 VARCHAR(10) NOT NULL,
  state VARCHAR(20) NOT NULL,
  kickoff_time TIMESTAMPTZ,
  last_checked TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_games_state ON games(state);
CREATE INDEX IF NOT EXISTS idx_games_sport_state ON games(sport, state);

-- Table: health_check
CREATE TABLE IF NOT EXISTS health_check (
  id BIGSERIAL PRIMARY KEY,
  collector_name VARCHAR(50) NOT NULL UNIQUE,
  last_run TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status VARCHAR(20) NOT NULL,
  error_msg TEXT
);

-- Retention maintenance (prunes alerts older than 30 days)
CREATE OR REPLACE FUNCTION prune_old_alerts() RETURNS void AS $$
BEGIN
  DELETE FROM alerts WHERE created_at < NOW() - INTERVAL '30 days';
END;
$$ LANGUAGE plpgsql;
