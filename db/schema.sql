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
--    Free-text context provided by the user in plain English.
--    The agent reads ALL active entries and reasons about them.
--    No forced structure — user types whatever is relevant.
--
--    Examples of what users type:
--      "There will be a power down on 3rd March"
--      "New movie releases 9th April, expecting huge traffic"
--      "Flash sale runs every Friday 6-9 PM"
--      "Deployment of v3.0 tonight at 11 PM"
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_store (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,

    -- Auto-generated key based on timestamp — user never sees this
    key         TEXT NOT NULL UNIQUE,

    -- Raw free-text exactly as the user typed it
    value       TEXT NOT NULL,

    source      TEXT NOT NULL CHECK (source IN ('user_provided', 'agent_question', 'system')),
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- NULL = never expires. Agent decides relevance, not expiry.
    expires_at  TIMESTAMPTZ,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_context_store_active
    ON context_store (is_active, added_at DESC)
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


-- ------------------------------------------------------------
-- 10. AGENT MEMORY PATTERNS (long-term structured memory)
--    One row per recognised failure pattern per service.
--    Updated/merged after each agent run.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_memory_patterns (
    id                      UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    service_name            TEXT NOT NULL,

    -- What kind of pattern this is
    pattern_type            TEXT NOT NULL,
    -- e.g. "response_time_spike", "error_rate_surge",
    --      "gradual_degradation", "cascade_failure", "traffic_surge"

    -- When this pattern tends to occur
    time_of_day             TEXT,   -- "morning", "afternoon", "evening", "overnight"
    day_of_week             TEXT,   -- "weekday", "weekend", "friday", or NULL if any

    -- What the agent concluded
    root_cause              TEXT NOT NULL,
    resolution              TEXT,   -- what fixed it last time
    resolution_time_mins    INT,    -- how long it took to resolve

    -- How reliable this pattern is
    occurrence_count        INT NOT NULL DEFAULT 1,
    prediction_correct_count INT NOT NULL DEFAULT 0,
    prediction_total_count  INT NOT NULL DEFAULT 0,

    -- Outcome of last prediction
    last_outcome            TEXT,   -- "correct", "incorrect", "unknown"
    last_seen               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_seen              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Plain text summary for LLM injection
    raw_summary             TEXT NOT NULL,

    -- Link to the agent output that created/updated this
    source_output_id        UUID REFERENCES agent_outputs(id),

    UNIQUE (service_name, pattern_type, time_of_day)
);

CREATE INDEX IF NOT EXISTS idx_memory_patterns_service
    ON agent_memory_patterns (service_name, last_seen DESC);


-- ------------------------------------------------------------
-- 11. AGENT MEMORY RETRIEVALS (audit log)
--    Records exactly which memories were used in each agent run.
--    This is what gets displayed to the user — "memories explored".
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_memory_retrievals (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    agent_output_id     UUID REFERENCES agent_outputs(id),
    retrieved_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    memory_type         TEXT NOT NULL,  -- "short_term" | "long_term"
    service_name        TEXT,
    memory_summary      TEXT NOT NULL,  -- what was retrieved
    relevance_reason    TEXT            -- why this memory was selected
);

CREATE INDEX IF NOT EXISTS idx_memory_retrievals_output
    ON agent_memory_retrievals (agent_output_id);

-- ------------------------------------------------------------
-- 10. VECTOR SEARCH ON AGENT OUTPUTS
--     Adds semantic search capability to agent_outputs.
--     Run this after enabling pgvector extension.
-- ------------------------------------------------------------
ALTER TABLE agent_outputs
ADD COLUMN IF NOT EXISTS signal_embedding vector(384),
ADD COLUMN IF NOT EXISTS signal_text TEXT;

CREATE INDEX IF NOT EXISTS idx_agent_outputs_embedding
ON agent_outputs
USING ivfflat (signal_embedding vector_cosine_ops)
WITH (lists = 10);