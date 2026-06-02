-- ============================================================
-- SRE Agent · Supabase Schema
-- Run this in your Supabase SQL editor to set up all tables
-- ============================================================


-- ------------------------------------------------------------
-- 1. RAW METRICS
--    One row per service per collector ping (every 60s)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metrics_raw (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    service_name        TEXT NOT NULL,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Core health metrics
    response_time_ms    FLOAT,
    error_rate_pct      FLOAT,          -- 0.0 to 100.0
    throughput_rps      FLOAT,          -- requests per second
    uptime_pct          FLOAT,          -- 0.0 to 100.0
    upload_time_ms      FLOAT,          -- time to complete a POST/upload
    cpu_pct             FLOAT,          -- 0.0 to 100.0
    memory_pct          FLOAT,          -- 0.0 to 100.0

    -- HTTP response info
    status_code         INT,
    is_reachable        BOOLEAN NOT NULL DEFAULT TRUE,

    -- Optional: raw error message if service returned one
    error_message       TEXT
);

-- Index for fast range queries (most common query pattern)
CREATE INDEX IF NOT EXISTS idx_metrics_raw_service_time
    ON metrics_raw (service_name, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_metrics_raw_timestamp
    ON metrics_raw (timestamp DESC);


-- ------------------------------------------------------------
-- 2. BASELINE PROFILES
--    Rolling mean + std dev per service per time window
--    Updated by the anomaly engine after each poll batch
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS baseline_profiles (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    service_name    TEXT NOT NULL,

    -- Time window this baseline applies to
    -- e.g. "weekday_morning", "weekday_afternoon", "weekend"
    time_window     TEXT NOT NULL,
    hour_start      INT NOT NULL,   -- 0-23
    hour_end        INT NOT NULL,   -- 0-23

    sample_count    INT NOT NULL DEFAULT 0,

    -- Per-metric statistics (mean and std dev)
    rt_mean         FLOAT,   -- response_time_ms
    rt_std          FLOAT,
    er_mean         FLOAT,   -- error_rate_pct
    er_std          FLOAT,
    tp_mean         FLOAT,   -- throughput_rps
    tp_std          FLOAT,
    cpu_mean        FLOAT,
    cpu_std         FLOAT,
    mem_mean        FLOAT,
    mem_std         FLOAT,

    last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (service_name, time_window)
);


-- ------------------------------------------------------------
-- 3. ANOMALY EVENTS
--    Raw anomaly detections with full z-score breakdown
--    Written by the anomaly engine when a signal is detected
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS anomaly_events (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    service_name    TEXT NOT NULL,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Worst single z-score across all metrics (for quick sorting)
    max_z_score     FLOAT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),

    -- Full breakdown: array of {metric, current_value, mean, std, z_score}
    anomalies       JSONB NOT NULL DEFAULT '[]',

    -- Trend information
    trend           JSONB NOT NULL DEFAULT '{}',
    -- Example:
    -- {
    --   "response_time_ms": {
    --     "direction": "increasing",
    --     "rate_per_min": 38.4,
    --     "duration_mins": 42,
    --     "is_accelerating": true
    --   }
    -- }

    -- Which other services are also degrading at this moment
    correlated_services JSONB NOT NULL DEFAULT '[]',

    -- Whether this anomaly has been processed by the agent
    processed_by_agent  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_anomaly_service_time
    ON anomaly_events (service_name, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_anomaly_unprocessed
    ON anomaly_events (processed_by_agent, detected_at DESC)
    WHERE processed_by_agent = FALSE;


-- ------------------------------------------------------------
-- 4. LLM SIGNALS
--    Formatted, human-readable signals ready for LLM input
--    Produced by the signal formatter from anomaly_events
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_signals (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    anomaly_event_id    UUID REFERENCES anomaly_events(id),
    service_name        TEXT NOT NULL,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    severity            TEXT NOT NULL,

    -- Plain English summary (goes directly into LLM prompt)
    human_summary       TEXT NOT NULL,

    -- Structured snapshot for LLM context
    metrics_snapshot    JSONB NOT NULL DEFAULT '{}',
    -- Example:
    -- {
    --   "response_time_ms": {"now": 847, "normal_range": "355-465ms", "z_score": 15.3},
    --   "error_rate_pct":   {"now": 3.2,  "normal_range": "0.4-1.2%",  "z_score": 6.0}
    -- }

    trend_summary       TEXT,
    context_window      TEXT,   -- e.g. "Wednesday afternoon, historically moderate load"
    hypothesis_hints    JSONB NOT NULL DEFAULT '[]',
    correlated_services JSONB NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_llm_signals_service_time
    ON llm_signals (service_name, generated_at DESC);


-- ------------------------------------------------------------
-- 5. CONTEXT PACKAGES
--    Full bundle sent to the agent on each invocation
--    Stored for audit, replay, and health query answering
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_packages (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trigger_reason  TEXT,   -- "anomaly_detected" | "scheduled" | "user_query" | "simulate"

    -- The full package serialised — used for LLM prompt construction
    llm_signal      JSONB,
    recent_metrics  JSONB,      -- last 30 min snapshot array
    historical      JSONB,      -- 7-day same-window summary
    dependency_map  JSONB,
    context_store   JSONB,      -- snapshot of active context entries at call time
    datetime_ctx    JSONB       -- {iso, day_of_week, time_of_day, days_to_weekend, ...}
);

CREATE INDEX IF NOT EXISTS idx_context_packages_time
    ON context_packages (created_at DESC);


-- ------------------------------------------------------------
-- 6. AGENT OUTPUTS
--    Every structured response the LLM returns
--    Used for: history queries, trend review, audit
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_outputs (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    context_package_id  UUID REFERENCES context_packages(id),
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode                TEXT NOT NULL,
    -- modes: rca | predict_degradation | load_prediction |
    --        alert_grouping | health_query | blast_radius

    service_name        TEXT,
    confidence          INT,            -- 0-100
    needed_more_context BOOLEAN DEFAULT FALSE,
    context_question    TEXT,           -- question agent asked user (if any)
    user_answer         TEXT,           -- answer user gave (if any)

    -- Full structured output
    rca                 JSONB,
    prediction          JSONB,
    load_prediction     JSONB,
    blast_radius        JSONB,
    alert_group         JSONB,
    fix_suggestions     JSONB,

    -- Raw LLM response stored for debugging / model comparison
    raw_llm_response    TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_outputs_time
    ON agent_outputs (generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_outputs_service
    ON agent_outputs (service_name, generated_at DESC);


-- ------------------------------------------------------------
-- 7. CONTEXT STORE
--    Pre-fed and user-answered context with optional expiry
--    This is the agent's persistent memory across sessions
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_store (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    value       TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('user_provided', 'agent_question', 'system')),
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ,    -- NULL = never expires
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_context_store_active
    ON context_store (is_active, expires_at)
    WHERE is_active = TRUE;


-- ------------------------------------------------------------
-- 8. ALERTS
--    Individual alerts before noise reduction/grouping
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    service_name    TEXT NOT NULL,
    anomaly_id      UUID REFERENCES anomaly_events(id),
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric          TEXT NOT NULL,      -- which metric triggered this
    severity        TEXT NOT NULL,
    message         TEXT NOT NULL,
    is_grouped      BOOLEAN NOT NULL DEFAULT FALSE,
    incident_id     UUID                -- set after grouping
);

CREATE INDEX IF NOT EXISTS idx_alerts_service_time
    ON alerts (service_name, triggered_at DESC);

CREATE INDEX IF NOT EXISTS idx_alerts_ungrouped
    ON alerts (is_grouped, triggered_at DESC)
    WHERE is_grouped = FALSE;


-- ------------------------------------------------------------
-- 9. INCIDENTS
--    Grouped alerts after noise reduction (many alerts → 1)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    title           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','investigating','resolved')),

    affected_services   JSONB NOT NULL DEFAULT '[]',
    raw_alert_count     INT NOT NULL DEFAULT 1,
    suppressed_count    INT NOT NULL DEFAULT 0,
    agent_output_id     UUID REFERENCES agent_outputs(id)
);


-- ------------------------------------------------------------
-- HELPER VIEW: latest metric per service
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW latest_metrics AS
SELECT DISTINCT ON (service_name)
    service_name,
    timestamp,
    response_time_ms,
    error_rate_pct,
    throughput_rps,
    uptime_pct,
    cpu_pct,
    memory_pct,
    is_reachable,
    status_code
FROM metrics_raw
ORDER BY service_name, timestamp DESC;
