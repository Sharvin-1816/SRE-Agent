-- ============================================================
-- Supabase RPC Functions
-- Run this in your Supabase SQL editor AFTER schema.sql
-- ============================================================


-- get_historical_window
-- Returns metric rows for the same time-of-day window
-- over the last N days. Used by baseline engine + context builder.
--
-- Example: all readings between 12:00-17:00 for last 7 days
--          for payment_service
CREATE OR REPLACE FUNCTION get_historical_window(
    p_service    TEXT,
    p_hour_start INT,
    p_hour_end   INT,
    p_days_back  INT DEFAULT 7
)
RETURNS TABLE (
    id                UUID,
    service_name      TEXT,
    timestamp         TIMESTAMPTZ,
    response_time_ms  FLOAT,
    error_rate_pct    FLOAT,
    throughput_rps    FLOAT,
    uptime_pct        FLOAT,
    upload_time_ms    FLOAT,
    cpu_pct           FLOAT,
    memory_pct        FLOAT,
    is_reachable      BOOLEAN
)
LANGUAGE sql STABLE
AS $$
    SELECT
        id, service_name, timestamp,
        response_time_ms, error_rate_pct, throughput_rps,
        uptime_pct, upload_time_ms, cpu_pct, memory_pct,
        is_reachable
    FROM metrics_raw
    WHERE
        service_name = p_service
        AND timestamp >= NOW() - (p_days_back || ' days')::INTERVAL
        AND EXTRACT(HOUR FROM timestamp AT TIME ZONE 'UTC') >= p_hour_start
        AND EXTRACT(HOUR FROM timestamp AT TIME ZONE 'UTC') <  p_hour_end
    ORDER BY timestamp DESC;
$$;


-- find_similar_memory_pattern
-- Uses pgvector cosine similarity to find semantically similar
-- memory patterns. Core of the deduplication system.
CREATE OR REPLACE FUNCTION find_similar_memory_pattern(
    p_service    TEXT,
    p_embedding  vector(384),
    p_threshold  FLOAT DEFAULT 0.85,
    p_limit      INT   DEFAULT 1
)
RETURNS TABLE (
    id                  UUID,
    service_name        TEXT,
    pattern_type        TEXT,
    root_cause          TEXT,
    resolution          TEXT,
    occurrence_count    INT,
    last_seen           TIMESTAMPTZ,
    outcome             TEXT,
    prediction_was_correct BOOLEAN,
    raw_summary         TEXT,
    time_of_day         TEXT,
    day_of_week         TEXT,
    similarity          FLOAT
)
LANGUAGE sql STABLE
AS $$
    SELECT
        id, service_name, pattern_type,
        root_cause, resolution, occurrence_count,
        last_seen, outcome, prediction_was_correct,
        raw_summary, time_of_day, day_of_week,
        1 - (root_cause_embedding <=> p_embedding) AS similarity
    FROM agent_memory_patterns
    WHERE
        service_name = p_service
        AND root_cause_embedding IS NOT NULL
        AND 1 - (root_cause_embedding <=> p_embedding) >= p_threshold
    ORDER BY root_cause_embedding <=> p_embedding
    LIMIT p_limit;
$$;
-- Used by the NL health query module.
-- Returns per-service aggregate stats for a given time range.
CREATE OR REPLACE FUNCTION get_service_health_summary(
    p_since TIMESTAMPTZ,
    p_until TIMESTAMPTZ
)
RETURNS TABLE (
    service_name        TEXT,
    avg_response_time   FLOAT,
    max_response_time   FLOAT,
    avg_error_rate      FLOAT,
    max_error_rate      FLOAT,
    min_uptime          FLOAT,
    total_readings      BIGINT,
    unreachable_count   BIGINT
)
LANGUAGE sql STABLE
AS $$
    SELECT
        service_name,
        ROUND(AVG(response_time_ms)::NUMERIC, 2)  AS avg_response_time,
        ROUND(MAX(response_time_ms)::NUMERIC, 2)  AS max_response_time,
        ROUND(AVG(error_rate_pct)::NUMERIC,   2)  AS avg_error_rate,
        ROUND(MAX(error_rate_pct)::NUMERIC,   2)  AS max_error_rate,
        ROUND(MIN(uptime_pct)::NUMERIC,       2)  AS min_uptime,
        COUNT(*)                                   AS total_readings,
        COUNT(*) FILTER (WHERE is_reachable = FALSE) AS unreachable_count
    FROM metrics_raw
    WHERE timestamp BETWEEN p_since AND p_until
    GROUP BY service_name
    ORDER BY avg_response_time DESC;
$$;


-- match_agent_outputs
-- Semantic similarity search over agent_outputs using pgvector.
-- Finds past agent decisions most similar to the current anomaly signal.
-- Used by the intelligent memory retrieval system.
CREATE OR REPLACE FUNCTION match_agent_outputs(
    p_service    TEXT,
    p_embedding  vector(384),
    p_mode       TEXT DEFAULT NULL,
    p_threshold  FLOAT DEFAULT 0.70,
    p_limit      INT   DEFAULT 5
)
RETURNS TABLE (
    id              UUID,
    service_name    TEXT,
    mode            TEXT,
    confidence      INT,
    signal_text     TEXT,
    rca             JSONB,
    prediction      JSONB,
    blast_radius    JSONB,
    fix_suggestions JSONB,
    generated_at    TIMESTAMPTZ,
    similarity      FLOAT
)
LANGUAGE sql STABLE
AS $$
    SELECT
        id, service_name, mode, confidence,
        signal_text, rca, prediction, blast_radius,
        fix_suggestions, generated_at,
        1 - (signal_embedding <=> p_embedding) AS similarity
    FROM agent_outputs
    WHERE
        service_name = p_service
        AND signal_embedding IS NOT NULL
        AND (p_mode IS NULL OR mode = p_mode)
        AND 1 - (signal_embedding <=> p_embedding) >= p_threshold
    ORDER BY signal_embedding <=> p_embedding
    LIMIT p_limit;
$$;