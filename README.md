<h1 align="center">SRE Agent 🤖</h1>
<p align="center">
  <b>AI-powered microservice monitoring and decision-support system</b><br/>
  Predicts failures, performs root cause analysis, reduces alert noise,<br/>
  and estimates blast radius — before things go wrong.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/LLM-Groq%20%2F%20Ollama-blue?style=flat-square"/>
  <img src="https://img.shields.io/badge/DB-Supabase%20(PostgreSQL)-green?style=flat-square"/>
  <img src="https://img.shields.io/badge/Backend-Python-yellow?style=flat-square"/>
  <img src="https://img.shields.io/badge/Services-FastAPI-teal?style=flat-square"/>
  <img src="https://img.shields.io/badge/Status-Active-brightgreen?style=flat-square"/>
</p>

---

## What is this?

Most monitoring tools tell you **what broke**. This agent tells you **why it broke, what's about to break, and what to do about it** — before your users notice.

It monitors multiple microservices continuously, detects anomalies using statistical baselines (not hardcoded thresholds), feeds rich contextual signals to an LLM, and produces actionable analysis in plain English.

The agent is **context-aware**. You tell it things like:

> *"There will be a power outage on 3rd March from 2-6 AM"*  
> *"Buy 1 Get 1 offer starts in 2 days"*  
> *"New movie releases on 9th April — expecting huge traffic"*

The agent factors these into every prediction, RCA, and load forecast automatically. It doesn't just match patterns — it **reasons**.

---

## Features

| Feature | Description |
|---|---|
| **Automated RCA** | Determines why a service failed — rules out causes, identifies root source, suggests concrete fixes |
| **Predictive Degradation** | Predicts when a service will fail based on trend trajectory and upcoming events |
| **Load Prediction** | Forecasts future traffic using historical patterns + operator-provided event context |
| **Smart Alert Noise Reduction** | Groups N correlated alerts into M meaningful incidents, suppresses noise |
| **Natural Language Health Query** | Ask *"Which services were unstable this weekend?"* and get a direct answer |
| **Blast Radius Estimator** | When a service degrades, predicts which downstream services will be affected and by how much |
| **Operator Context** | User feeds plain-English events; agent uses them to reason about all predictions |
| **Agentic Loop** | If confidence is low, agent asks the user a targeted question before concluding |

---

## How the Agent Works

```
┌─────────────────────────────────────────────────────────┐
│           MOCK MICROSERVICES (FastAPI)                  │
│  Payment · Cart · Notification · Auth · Inventory · GW  │
│  Each exposes /health and /metrics                      │
└──────────────────────┬──────────────────────────────────┘
                       │ ping every 60s
                       ▼
┌─────────────────────────────────────────────────────────┐
│           METRICS COLLECTOR (APScheduler)               │
│  Stores raw metrics → Supabase                          │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│           ANOMALY DETECTION ENGINE                      │
│  Z-score baseline (per service, per time-of-day)        │
│  Trend detection (rate of change, acceleration)         │
│  Correlation check (are other services also degrading?) │
│  → Produces plain-English LLM signal                    │
└──────────────────────┬──────────────────────────────────┘
                       │ on anomaly or every 5 min
                       ▼
┌─────────────────────────────────────────────────────────┐
│           CONTEXT BUILDER                               │
│  Last 30min metrics · 7-day history                     │
│  Dependency map · Operator context · Date/time          │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│           AGENTIC LOOP                                  │
│  1. OBSERVE  — ingest full context package              │
│  2. REASON   — LLM first-pass analysis                  │
│  3. GAPS?    — confidence < 75%? Ask user via CLI       │
│  4. CONCLUDE — full structured JSON analysis            │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│           OUTPUT (6 MODULES)                            │
│  RCA · Degradation Prediction · Load Prediction         │
│  Alert Grouping · Health Query · Blast Radius           │
│  + Actionable fix suggestions for every incident        │
└─────────────────────────────────────────────────────────┘
```

### Why Z-score instead of thresholds?

A response time of 800ms at 3 AM is suspicious. The same 800ms during a Friday evening flash sale is normal. Fixed thresholds can't tell the difference. The anomaly engine builds a **rolling baseline per service per time-of-day window** and flags deviations in standard deviations — not absolute values. This means:

- Payment service (baseline 410ms) and Inventory service (baseline 620ms) have different normals
- 3 AM baseline and 6 PM baseline for the same service are different
- Gradual degradation over 40 minutes is caught even if no single reading spikes

### Why operator context matters

The LLM receives all user-provided context as a plain-text block in every prompt:

```
[2026-06-02] Buy 1 Get 1 offer starts in 2 days
[2026-06-01] Flash sale every Friday 6-9 PM, 3-4x load expected
[2026-06-01] Deployment of payment service v2.3 today at 12:15 PM
```

This lets the agent reason: *"Payment service is degrading AND a BOGO offer starts in 2 days — current trajectory will breach SLA before the sale. Scale now, not when it crashes."*

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Mock services | **FastAPI** (Python) | 6 simulated microservices with realistic failure modes |
| Scheduler | **APScheduler** | Polls services every 60s, triggers anomaly checks |
| Database | **Supabase (PostgreSQL)** | Time-series metrics, baselines, agent outputs |
| Anomaly detection | **Pure Python (Z-score + linear regression)** | Context-aware anomaly detection without ML libraries |
| LLM (primary) | **Groq API** — `llama-3.3-70b-versatile` | Fast, free-tier LLM inference |
| LLM (fallback) | **Ollama** — local Llama 3.1 | Offline fallback if Groq is unavailable |
| LLM adapter | **Custom Python swap layer** | Change LLM provider via one `.env` flag |
| Agent framework | **Pure Python agentic loop** | No LangChain — full control over reasoning steps |
| CLI | **Rich** (Python) | Beautiful terminal output and interactive prompts |

---

## Project Structure

```
sre_agent/
  db/
    schema.sql              # All 9 Supabase tables + indexes + views
    rpc_functions.sql       # Postgres functions for time-window queries
    database.py             # All DB read/write operations (single source of truth)
    seed.py                 # 7 days of realistic mock data

  collector/
    collector.py            # APScheduler — polls services, triggers agent
    anomaly_detector.py     # Z-score + trend detection → LLM signal formatter

  services/
    base_service.py         # Base class for all mock services
    definitions.py          # 6 service instances with realistic baselines
    service_runner.py       # Starts all 6 services simultaneously
    simulate_incident.py    # CLI tool to trigger demo incident scenarios

  agent/
    llm_adapter.py          # Groq/Ollama swap layer — one flag in .env
    prompts.py              # System prompts for all 6 reasoning modes
    context_builder.py      # Assembles full context package for LLM
    agent_loop.py           # Core loop: observe → reason → ask → conclude

  config/
    dependency_map.py       # Service dependency graph for blast radius

  main.py                   # CLI entry point — all user interaction
  .env.example              # Environment variable template
  requirements.txt
```

---

## Database Schema

| Table | Purpose |
|---|---|
| `metrics_raw` | One row per service per poll — the time-series core |
| `baseline_profiles` | Rolling mean + std dev per service per time window |
| `anomaly_events` | Detected anomalies with full z-score breakdown |
| `llm_signals` | Plain-English signals ready for LLM consumption |
| `context_packages` | Full bundles sent to agent — full audit trail |
| `agent_outputs` | Every LLM response stored — queryable history |
| `context_store` | Free-text operator context (the agent's memory) |
| `alerts` | Individual alerts before noise reduction |
| `incidents` | Grouped alerts after noise reduction |

---

## Getting Started

### Prerequisites
- Python 3.10+
- A free [Supabase](https://supabase.com) account
- A free [Groq](https://console.groq.com) API key

### Setup

**1. Clone the repo**
```bash
git clone https://github.com/your-username/sre-agent.git
cd sre-agent
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Set up Supabase**
- Create a new project at supabase.com
- Go to SQL Editor → paste and run `db/schema.sql`
- Go to SQL Editor → paste and run `db/rpc_functions.sql`

**4. Configure environment**
```bash
cp .env.example .env
# Fill in SUPABASE_URL, SUPABASE_ANON_KEY, GROQ_API_KEY
```

**5. Seed the database**
```bash
python db/seed.py
```

**6. Run**

Open 3 terminals:
```bash
# Terminal 1 — mock services
python -m services.service_runner

# Terminal 2 — collector + anomaly detection
python -m collector.collector

# Terminal 3 — agent CLI
python main.py
```

---

## CLI Commands

| Command | What it does |
|---|---|
| `context` | Add free-text operator context (events, outages, deployments) |
| `contexts` | View and manage all saved context entries |
| `status` | Live health table of all 6 services |
| `query` | Ask a natural language question about system health |
| `predict` | Run load and capacity prediction |
| `blast` | Estimate blast radius if a service fails |
| `alerts` | Run alert noise reduction on current alerts |
| `simulate` | Trigger an incident scenario for demo |

---

## Simulate an Incident

```bash
agent> simulate
```

Choose from 4 scenarios:

| Scenario | Tests |
|---|---|
| Payment gradual degradation | Predictive degradation + RCA |
| Cascading failure payment → cart | Blast radius estimator |
| Black Friday 5x surge | Load prediction |
| Gateway full outage | Alert noise reduction |

After triggering a scenario, wait up to 60 seconds for the collector to detect it and auto-trigger the full agent analysis.

---

## Extending to Real APIs

The mock services are a test harness. To point this at real APIs, change one section in `collector/collector.py`:

```python
SERVICES = {
    "your_payment_api":  "https://api.yourcompany.com/payment",
    "your_auth_api":     "https://api.yourcompany.com/auth",
    # ... add any service that exposes a /metrics or /health endpoint
}
```

Everything else — anomaly detection, baselines, agent reasoning — works identically on real data.

---

## Roadmap

- [ ] Frontend dashboard (React + Recharts)
- [ ] Webhook support for real alert ingestion (PagerDuty, Grafana)
- [ ] Support for real API metrics formats (Prometheus, OpenTelemetry)
- [ ] Scheduled proactive analysis (not just on anomaly)
- [ ] Multi-LLM comparison mode
- [ ] Slack/Teams notification integration

---

## Contributing

This project is actively evolving. If you find a bug or want to add a feature, open an issue or PR.

---

<p align="center">Built with Python · Groq · Supabase · FastAPI · Rich</p>
