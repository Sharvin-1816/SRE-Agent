<h1 align="center">SRE Agent</h1>
<p align="center">
  <b>AI-powered microservice monitoring and decision-support system</b><br/>
  Predicts failures, performs root cause analysis, reduces alert noise,<br/>
  and estimates blast radius — before things go wrong.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v3.0-blue?style=flat-square"/>
  <img src="https://img.shields.io/badge/LLM-Groq%20%2F%20Ollama-orange?style=flat-square"/>
  <img src="https://img.shields.io/badge/Framework-LangChain-blueviolet?style=flat-square"/>
  <img src="https://img.shields.io/badge/DB-Supabase%20%2B%20pgvector-green?style=flat-square"/>
  <img src="https://img.shields.io/badge/Metrics-Prometheus-red?style=flat-square"/>
  <img src="https://img.shields.io/badge/Dashboard-Grafana-yellow?style=flat-square"/>
  <img src="https://img.shields.io/badge/Backend-Python-blue?style=flat-square"/>
  <img src="https://img.shields.io/badge/Status-Active-brightgreen?style=flat-square"/>
</p>

---

## What is this?

Most monitoring tools tell you **what broke**. This agent tells you **why it broke, what is about to break, and what to do about it** — before your users notice.

It monitors multiple microservices continuously, detects anomalies using statistical baselines (not hardcoded thresholds), feeds rich contextual signals to an LLM, and produces actionable analysis in plain English.

The agent is **context-aware**. You tell it things like:

> *"There will be a power outage on 3rd March from 2-6 AM"*
> *"Buy 1 Get 1 offer starts in 2 days"*
> *"New movie releases on 9th April — expecting huge traffic"*

The agent factors these into every prediction, RCA, and load forecast automatically. It does not just match patterns — it **reasons**.

---

## Versions

### v3.0 — Current (LangChain Integration)
Full LangChain integration across the LLM and prompt layers. Manual `httpx` HTTP calls replaced with `ChatGroq` and `ChatOllama`. All 6 reasoning modes use `ChatPromptTemplate` objects. Embeddings migrated to `HuggingFaceEmbeddings` via `langchain-huggingface`. Pydantic output schemas added for structured LLM outputs.

### v2.2 — Webhook Support + pgvector Memory
Grafana alert webhooks trigger the agent directly. Memory deduplication upgraded to pgvector cosine similarity — semantically similar patterns are merged instead of creating duplicates. `sentence-transformers` replaced with `HuggingFaceEmbeddings`.

### v2.1 — Two-tier Memory
Added short-term (last 48h) and long-term (pgvector) memory. Pattern extraction after every agent run. Full reasoning transparency — agent displays which memories it drew from. New `memory` CLI command.

### v2.0 — Prometheus + Grafana
Full observability stack. Agent uses p50/p95/p99 latency from Prometheus instead of averages. Live Grafana dashboards show real-time service health.

### v1.0 — Base Agent
Core agentic loop with all 6 reasoning modes. Metrics sourced from Supabase only. Fully functional agent reasoning.

See [Releases](../../releases) to download any version.

---

## Features

| Feature | Description |
|---|---|
| **Automated RCA** | Determines why a service failed — rules out causes, identifies root source, suggests concrete fixes |
| **Predictive Degradation** | Predicts when a service will fail based on trend trajectory and upcoming events |
| **Load Prediction** | Forecasts future traffic using historical patterns and operator-provided event context |
| **Smart Alert Noise Reduction** | Groups N correlated alerts into M meaningful incidents, suppresses noise |
| **Natural Language Health Query** | Ask "Which services were unstable this weekend?" and get a direct answer |
| **Blast Radius Estimator** | When a service degrades, predicts which downstream services will be affected and by how much |
| **Operator Context** | User feeds plain-English events; agent uses them to reason about all predictions |
| **Agentic Loop** | If confidence is low, agent asks the user a targeted question before concluding |
| **Memory System** | Agent remembers past incidents and patterns across sessions — reasoning improves over time |
| **Grafana Webhooks** | Grafana alert rules trigger the agent automatically with deduplication |

---

## Architecture

```mermaid
flowchart TD
    subgraph SVC["Mock Microservices — FastAPI"]
        P["payment :3001"]
        C["cart :3002"]
        N["notification :3003"]
        A["auth :3004"]
        I["inventory :3005"]
        G["gateway :3006"]
    end

    subgraph OBS["Observability — Docker"]
        PROM["Prometheus\nscrape /metrics every 15s\np50 / p95 / p99 latency"]
        GRAF["Grafana\nlive dashboards\nalert rules + contact point"]
    end

    subgraph COL["Collection + Detection"]
        COLL["Collector — APScheduler\npoll /metrics/json every 60s\nwrites to Supabase"]
        ANOM["Anomaly Detector\nZ-score per service per time window\ntrend detection · LLM signal formatter"]
    end

    subgraph HOOK["Webhook Receiver — FastAPI :5001"]
        WH["POST /webhook/grafana\ndeduplication window\nsynthetic LLM signal builder"]
    end

    subgraph MEM["Memory System"]
        STM["Short term\nlast 48h agent decisions"]
        LTM["Long term patterns\npgvector · HuggingFaceEmbeddings\ncosine similarity deduplication"]
    end

    subgraph AGENT["Agentic Loop — Python + LangChain"]
        CB["Context Builder\nPrometheus metrics · Supabase history\noperator context · dependency map"]
        LOOP["1 observe · 2 reason · 3 ask if confidence lt 75% · 4 conclude"]
        LLM["LangChain — ChatGroq llama-3.3-70b\nfallback: ChatOllama local\nChatPromptTemplate · JsonOutputParser"]
    end

    subgraph OUT["Output — 6 Reasoning Modes"]
        RCA["RCA\nroot cause + fixes"]
        PRED["Prediction\ntime to failure"]
        LOAD["Load\ntraffic forecast"]
        ALRT["Alerts\nnoise reduction"]
        HQ["Health query\nnatural language"]
        BR["Blast radius\ncascade risk"]
    end

    subgraph DB["Supabase — PostgreSQL + pgvector"]
        T1["metrics_raw"]
        T2["agent_outputs"]
        T3["context_store"]
        T4["anomaly_events"]
        T5["incidents"]
        T6["agent_memory_patterns\npgvector 384-dim embeddings"]
    end

    SVC -->|"/metrics every 15s"| PROM
    SVC -->|"/metrics/json every 60s"| COLL
    PROM --> GRAF
    PROM --> CB
    GRAF -->|"alert fires"| WH
    COLL --> ANOM
    ANOM --> CB
    WH --> LOOP
    CB --> LOOP
    MEM --> LOOP
    LOOP --> LLM
    LLM --> OUT
    OUT --> DB
    OUT -->|"extract + store pattern"| LTM
    DB --> STM
    DB --> LTM
```

### Why Z-score instead of thresholds?

A response time of 800ms at 3 AM is suspicious. The same 800ms during a Friday evening flash sale is normal. Fixed thresholds cannot tell the difference. The anomaly engine builds a rolling baseline per service per time-of-day window and flags deviations in standard deviations — not absolute values.

### Why Prometheus and Supabase together?

| Prometheus | Supabase |
|---|---|
| Live p50/p95/p99 latency | Agent outputs — every LLM response stored |
| Real-time scraping every 15s | Anomaly events and z-score history |
| Powers Grafana dashboards | Baseline profiles per service |
| Agent live context | Operator context store |
| | Memory patterns with pgvector embeddings |

Prometheus gives the agent richer real-time data. Supabase gives it memory, history, and persistence. Neither replaces the other.

### Why operator context matters

The LLM receives all user-provided context as a plain-text block in every prompt:

```
[2026-06-02] Buy 1 Get 1 offer starts in 2 days
[2026-06-01] Flash sale every Friday 6-9 PM, 3-4x load expected
[2026-06-01] Deployment of payment service v2.3 today at 12:15 PM
```

This lets the agent reason: "Payment service is degrading AND a BOGO offer starts in 2 days — current trajectory will breach SLA before the sale. Scale now, not when it crashes."

### Why pgvector for memory deduplication?

The agent extracts a structured pattern after every RCA and prediction run. Without vector similarity, "DB connection pool exhaustion after deployment" and "database pool saturated by new queries" would be stored as two separate records. pgvector cosine similarity catches these as the same pattern (similarity > 0.85) and updates the existing record instead of inserting a duplicate — memory gets smarter over time instead of noisier.

Note: Chroma can also be used as an alternative vector store.

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Mock services | **FastAPI** (Python) | 6 simulated microservices with realistic failure modes |
| Metrics collection | **Prometheus** | Scrapes all services every 15s, stores p50/p95/p99 |
| Visualization | **Grafana** | Live dashboards — request rate, latency, errors, CPU |
| Scheduler | **APScheduler** | Polls services every 60s, runs anomaly detection |
| Database | **Supabase (PostgreSQL + pgvector)** | Agent outputs, baselines, context, memory patterns |
| Anomaly detection | **Pure Python (Z-score + linear regression)** | Context-aware, per-service, per-time-window |
| LLM (primary) | **Groq API** via `langchain-groq` — `llama-3.3-70b-versatile` | Fast, free-tier LLM inference |
| LLM (fallback) | **Ollama** via `langchain-ollama` — local Llama 3.1 | Offline fallback if Groq unavailable |
| LLM framework | **LangChain** — `langchain`, `langchain-core`, `langchain-groq`, `langchain-ollama`, `langchain-huggingface` | Unified LLM interface, prompt templates, structured output parsing |
| Embeddings | **HuggingFaceEmbeddings** — `all-MiniLM-L6-v2` (384-dim) | Local embeddings for pgvector memory deduplication |
| Agent framework | **Pure Python agentic loop** | Full control over observe → reason → ask → conclude |
| Webhooks | **FastAPI** on port 5001 | Receives Grafana alerts, triggers agent with deduplication |
| CLI | **Rich** (Python) | Terminal output and interactive prompts |
| Containers | **Docker** | Runs Prometheus and Grafana |

---

## Project Structure

```
sre_agent/
  docker-compose.yml            # Prometheus + Grafana containers

  db/
    schema.sql                  # All Supabase tables including agent_memory_patterns
    rpc_functions.sql           # Postgres functions including pgvector similarity search
    database.py                 # All DB operations — single source of truth
    seed.py                     # 7 days of realistic mock data

  collector/
    collector.py                # APScheduler — polls services, triggers agent on anomaly
    anomaly_detector.py         # Z-score + trend detection — produces LLM-ready signals

  services/
    base_service.py             # Base class with Prometheus metrics + background simulator
    definitions.py              # 6 service instances with realistic baselines
    service_runner.py           # Starts all 6 services simultaneously
    simulate_incident.py        # CLI tool to trigger demo incident scenarios

  agent/
    llm_adapter.py              # LangChain — ChatGroq/ChatOllama, ask_llm, ask_llm_from_template
    prompts.py                  # System prompt strings + ChatPromptTemplate objects for all 6 modes
    context_builder.py          # Assembles full context — Prometheus + Supabase + operator context
    prometheus_adapter.py       # Queries Prometheus HTTP API with PromQL
    agent_loop.py               # Core loop: observe → reason → ask → conclude
    memory.py                   # Two-tier memory — short term (48h) + long term patterns
    embeddings.py               # HuggingFaceEmbeddings wrapper for pgvector deduplication

  api/
    webhook_receiver.py         # FastAPI on :5001 — receives Grafana alerts, triggers agent
    decisions_api.py            # FastAPI on :5000 — exposes agent outputs for Grafana (parked)
    parsers/
      grafana.py                # Parses Grafana unified alerting webhook payload
      normaliser.py             # Converts all alert formats to internal schema

  config/
    dependency_map.py           # Service dependency graph for blast radius calculation
    prometheus.yml              # Prometheus scrape config — all 6 services
    grafana/
      provisioning/
        datasources/            # Auto-connects Grafana to Prometheus
        dashboards/             # Pre-built SRE Agent service overview dashboard

  main.py                       # CLI entry point — all user interaction
  .env.example                  # Environment variable template
  requirements.txt
```

---

## Getting Started

### Prerequisites
- Python 3.10+
- Docker Desktop
- A free [Supabase](https://supabase.com) account
- A free [Groq](https://console.groq.com) API key

### Setup

**1. Clone the repo**
```bash
git clone https://github.com/Sharvin-1816/SRE-Agent.git
cd SRE-Agent
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Set up Supabase**
- Create a new project at supabase.com
- Go to SQL Editor → paste and run `db/schema.sql`
- Go to SQL Editor → paste and run `db/rpc_functions.sql`
- Enable pgvector: `CREATE EXTENSION IF NOT EXISTS vector;`
- Add memory embedding column: `ALTER TABLE agent_memory_patterns ADD COLUMN IF NOT EXISTS root_cause_embedding vector(384);`

**4. Configure environment**
```bash
cp .env.example .env
# Fill in SUPABASE_URL, SUPABASE_SERVICE_KEY, GROQ_API_KEY
```

**5. Seed the database**
```bash
python db/seed.py
```

**6. Start Prometheus + Grafana**
```bash
docker-compose up -d
```

**7. Run everything**

Open 3 terminals:
```bash
# Terminal 1 — mock services
python -m services.service_runner

# Terminal 2 — collector + anomaly detection
python -m collector.collector

# Terminal 3 — agent CLI
python main.py
```

**8. Optional — Start webhook receiver**
```bash
# Terminal 4 — Grafana alert webhooks
python -m api.webhook_receiver
```

**9. Open Grafana**
- Go to `http://localhost:3000`
- Login: `admin` / `sreagent`
- Dashboards → SRE Agent → Service Overview

---

## CLI Commands

| Command | What it does |
|---|---|
| `context` | Add free-text operator context (events, outages, deployments) |
| `contexts` | View and manage all saved context entries |
| `status` | Live health table — shows if Prometheus and Tempo are active |
| `query` | Ask a natural language question about system health |
| `predict` | Run load and capacity prediction |
| `blast` | Estimate blast radius if a service fails |
| `alerts` | Run alert noise reduction on current alerts |
| `simulate` | Trigger an incident scenario for demo |
| `memory` | View all stored long term memory patterns |
| `webhooks` | Show webhook receiver status and recent activity |

---

## Simulate an Incident

```bash
agent> simulate
```

| Scenario | Tests |
|---|---|
| Payment gradual degradation | Predictive degradation + RCA |
| Cascading failure payment → cart | Blast radius estimator |
| Black Friday 5x surge | Load prediction |
| Gateway full outage | Alert noise reduction |

After triggering, watch Grafana for the spike and Terminal 2 for the agent's full analysis.

---

## Extending to Real APIs

Change one section in `collector/collector.py`:

```python
SERVICES = {
    "your_payment_api":  "https://api.yourcompany.com/payment",
    "your_auth_api":     "https://api.yourcompany.com/auth",
}
```

Update `config/prometheus.yml` targets to point to your real service `/metrics` endpoints. Everything else — anomaly detection, baselines, agent reasoning, memory — works identically on real data.

---

## Changelog

### v3.0
- Integrated LangChain across the LLM and prompt layers
- Replaced manual `httpx` Groq and Ollama HTTP calls with `ChatGroq` and `ChatOllama`
- Introduced `_invoke_with_fallback(messages)` shared core — both `ask_llm` and `ask_llm_from_template` use it
- Added `ask_llm_from_template(template, context)` for `ChatPromptTemplate`-based invocation
- Added `ask_llm_structured(system, user, schema)` with `JsonOutputParser` for typed outputs
- Added Pydantic output schemas: `RCAOutput`, `PredictionOutput`, `BlastRadiusOutput`
- Converted all 6 reasoning mode system prompts to `ChatPromptTemplate` objects
- All 6 mode runners in `agent_loop.py` now use `ask_llm_from_template`
- Replaced `sentence-transformers` `SentenceTransformer` with `HuggingFaceEmbeddings` from `langchain-huggingface` — same model, same public interface
- Removed incomplete OpenTelemetry integration
- Fixed duplicate `_metrics_source_label()` in `context_builder.py`
- Fixed numpy Python 3.13 wheel compatibility
- Fixed supabase and httpx version conflict

### v2.2
- Added Grafana webhook support — alert rules trigger the agent directly via `POST /webhook/grafana`
- Deduplication window (5 min per service) prevents rate limit hammering
- Grafana parsers normalise alert payloads to internal format
- pgvector cosine similarity for memory deduplication — similar patterns merged instead of duplicated
- `HuggingFaceEmbeddings` replaces direct `SentenceTransformer` for memory embeddings
- New `webhooks` CLI command shows receiver status and recent activity
- Database client now supports both `SUPABASE_SERVICE_KEY` and `SUPABASE_ANON_KEY`

### v2.1
- Added two-tier memory system — short term (last 48h) and long term patterns
- Short term: recent agent decisions injected into every RCA and prediction prompt
- Long term: structured patterns extracted after each run, stored in Supabase
- Agent displays which past memories it drew from after every analysis
- New `memory` CLI command shows all stored long term patterns
- Pattern extraction uses a secondary LLM call to build structured records
- New Supabase table: `agent_memory_patterns`

### v2.0
- Added Prometheus integration — scrapes all 6 services every 15s
- Agent uses p50/p95/p99 latency instead of averages
- Added Grafana dashboards (request rate, latency percentiles, CPU, memory, errors)
- Added `prometheus_adapter.py` — PromQL query layer with Supabase fallback
- Services expose `/metrics` (Prometheus format) and `/metrics/json` (collector)
- Fixed UUID truncation bug in alert grouping
- Fixed Groq rate limiting — 4s delay between LLM calls
- Agent triggers on worst anomaly only, not all 6 simultaneously

### v1.0
- Initial release
- 6 mock FastAPI microservices with configurable failure modes
- APScheduler-based metrics collector
- Z-score + trend anomaly detection
- Agentic loop: observe → reason → ask user → conclude
- 6 reasoning modes: RCA, prediction, load, alerts, health query, blast radius
- Operator context system — user feeds plain-English events
- Supabase (PostgreSQL) for all persistence
- Groq API (LLM) with Ollama fallback
- Alert noise reduction (N alerts → M incidents)
- Simulate incident CLI (4 scenarios)

---

## Roadmap

- [x] Prometheus + Grafana observability
- [x] Two-tier memory (short term + long term with pgvector)
- [x] Grafana webhook integration
- [x] LangChain integration (prompt templates, model wrappers, structured outputs)
- [ ] Agent decision panel in Grafana
- [ ] Scheduled proactive analysis (not just on anomaly)
- [ ] OpenTelemetry distributed tracing
- [ ] Memory outcome tracking — collector auto-updates prediction accuracy
- [ ] Slack/Teams notification integration

---

<p align="center">Built with Python · LangChain · Groq · Supabase · pgvector · Prometheus · Grafana · FastAPI · Rich</p>