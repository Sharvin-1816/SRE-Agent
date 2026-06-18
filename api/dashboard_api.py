"""
api/dashboard_api.py

The API layer behind the custom dashboard website. Wraps every agent
reasoning mode (RCA, prediction, blast radius, load, alerts, health query)
as an async job so the frontend never blocks on a slow LLM call.

Why async jobs instead of a direct blocking endpoint:
  LLM calls take 3-15 seconds. A blocking HTTP request that long is a bad
  experience for a website (no spinner, no "agent is thinking" animation,
  some browsers/proxies time out around 30-60s anyway). Instead:

    1. POST /api/jobs/{mode}      -> returns {"job_id": "..."} immediately
    2. GET  /api/jobs/{job_id}    -> {"status": "running"} while in progress
                                      {"status": "done", "result": {...}} when finished
                                      {"status": "error", "error": "..."} on failure

  The frontend polls step 2 every ~1s and shows a thinking animation while
  status == "running". This also gives us a natural place to later stream
  intermediate "agent is thinking: ..." text without changing the contract.

Jobs are kept in memory (a dict) since this API and the agent run in the
same process/host for now. If we split this across machines later, jobs
move to Redis or a Supabase table instead — the polling contract on the
frontend side does not need to change.

Run standalone:
  python -m api.dashboard_api
Runs on port 5050 by default.
"""

import os
import sys
import uuid
import threading
import traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from agent.logger import get_logger as _get_logger

_log = _get_logger("agent")

app = FastAPI(title="SRE Agent Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SERVICES = [
    "payment_service", "cart_service", "notification_service",
    "auth_service", "inventory_service", "gateway_service",
]


# ── Job store ──────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    QUEUED  = "queued"
    RUNNING = "running"
    DONE    = "done"
    ERROR   = "error"


_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# How long to keep finished jobs around before they're evicted (seconds).
# Prevents unbounded memory growth on a long-running dashboard API process.
JOB_TTL_SECONDS = int(os.getenv("DASHBOARD_JOB_TTL_SECONDS", "1800"))

# Caps how many LLM calls can be in flight at once across ALL dashboard
# jobs combined. Groq's free tier has a tokens-per-minute limit, and the
# collector's own run_agent() path already respects this via sleep delays
# between its sequential calls — but dashboard jobs are independent and
# can be fired concurrently by the frontend (e.g. two panels loading at
# once, or a user clicking multiple buttons quickly). Without a cap here,
# nothing stops 5+ simultaneous jobs from each making a Groq call at the
# same instant and tripping a 429. A job waiting on this semaphore still
# shows status "running" to the frontend — the thinking animation keeps
# working, it just means "running" now covers both "queued behind the
# concurrency limit" and "actively waiting on the LLM", which is fine
# since the dashboard doesn't need to distinguish those two sub-states.
MAX_CONCURRENT_LLM_CALLS = int(os.getenv("DASHBOARD_MAX_CONCURRENT_LLM_CALLS", "2"))
_llm_semaphore = threading.Semaphore(MAX_CONCURRENT_LLM_CALLS)


def _new_job(mode: str, params: dict) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "id":         job_id,
            "mode":       mode,
            "params":     params,
            "status":     JobStatus.QUEUED,
            "result":     None,
            "error":      None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }
    return job_id


def _set_job(job_id: str, **fields):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _evict_stale_jobs():
    """Drop finished jobs older than JOB_TTL_SECONDS. Called opportunistically."""
    now = datetime.now(timezone.utc)
    with _jobs_lock:
        stale = [
            jid for jid, j in _jobs.items()
            if j["status"] in (JobStatus.DONE, JobStatus.ERROR)
            and j.get("finished_at")
            and (now - datetime.fromisoformat(j["finished_at"])).total_seconds() > JOB_TTL_SECONDS
        ]
        for jid in stale:
            del _jobs[jid]


# ── Request models ────────────────────────────────────────────────────────

class BlastRadiusRequest(BaseModel):
    service_name: str


class PredictionRequest(BaseModel):
    service_name: str


class RCARequest(BaseModel):
    service_name: str


class LoadPredictionRequest(BaseModel):
    service_name: Optional[str] = None


class HealthQueryRequest(BaseModel):
    question: str


# ── Job runner ─────────────────────────────────────────────────────────────
# Each runner disables the CLI's blocking "ask user a question" behavior —
# there is no terminal to type into from a web request. Instead, if the
# agent would normally ask a clarifying question, we let it proceed with
# whatever confidence it already has and surface that fact in the result
# so the dashboard can show "agent was uncertain here" instead of hanging.

def _run_job(job_id: str, mode: str, fn, *args, **kwargs):
    _set_job(job_id, status=JobStatus.RUNNING)
    _log.info("Dashboard job started", job_id=job_id, mode=mode)

    # Disable the agent's interactive question-asking — but ONLY for this
    # thread. agent_loop.py stores this as thread-local state, so two jobs
    # running concurrently (allowed by MAX_CONCURRENT_LLM_CALLS >= 2) can
    # never interfere with each other's interactive/non-interactive flag,
    # and the CLI (running on a different thread entirely) is never
    # affected by anything a dashboard job does.
    import agent.agent_loop as agent_loop_module
    agent_loop_module.set_interactive(False)

    try:
        # Block here if MAX_CONCURRENT_LLM_CALLS jobs are already mid-LLM-call.
        # The frontend still sees status="running" the whole time, so the
        # thinking animation doesn't need to know about this queueing at all.
        with _llm_semaphore:
            result = fn(*args, **kwargs)
        _set_job(
            job_id,
            status=JobStatus.DONE,
            result=result,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        _log.info("Dashboard job completed", job_id=job_id, mode=mode)
    except Exception as e:
        _set_job(
            job_id,
            status=JobStatus.ERROR,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        _log.error(
            "Dashboard job failed",
            job_id=job_id, mode=mode, error=str(e),
            trace=traceback.format_exc()[:2000],
        )
    # No finally/restore needed — this thread's interactivity setting is
    # thread-local and the thread itself is discarded once the job ends.


def _start_job(mode: str, params: dict, fn, *args, **kwargs) -> str:
    job_id = _new_job(mode, params)
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, mode, fn, *args),
        kwargs=kwargs,
        daemon=True,
        name=f"job-{mode}-{job_id[:8]}",
    )
    thread.start()
    return job_id


def _validate_service(service_name: str):
    if service_name not in SERVICES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown service '{service_name}'. Valid: {SERVICES}",
        )


# ── Endpoints — trigger a job ─────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "service": "SRE Agent Dashboard API"}


@app.get("/api/services")
def list_services():
    """Service names the dashboard can show in dropdowns/selectors."""
    return {"services": SERVICES}


@app.post("/api/jobs/rca")
def trigger_rca(req: RCARequest):
    _validate_service(req.service_name)
    from agent.agent_loop import run_rca
    from db.database import get_latest_signal

    signal = get_latest_signal(req.service_name) or {}
    job_id = _start_job(
        "rca", req.model_dump(), run_rca, req.service_name, signal
    )
    return {"job_id": job_id}


@app.post("/api/jobs/prediction")
def trigger_prediction(req: PredictionRequest):
    _validate_service(req.service_name)
    from agent.agent_loop import run_prediction
    from db.database import get_latest_signal

    signal = get_latest_signal(req.service_name) or {}
    job_id = _start_job(
        "prediction", req.model_dump(), run_prediction, req.service_name, signal
    )
    return {"job_id": job_id}


@app.post("/api/jobs/blast-radius")
def trigger_blast_radius(req: BlastRadiusRequest):
    _validate_service(req.service_name)
    from agent.agent_loop import run_blast_radius

    job_id = _start_job(
        "blast_radius", req.model_dump(), run_blast_radius, req.service_name
    )
    return {"job_id": job_id}


@app.post("/api/jobs/load-prediction")
def trigger_load_prediction(req: LoadPredictionRequest):
    if req.service_name:
        _validate_service(req.service_name)
    from agent.agent_loop import run_load_prediction

    job_id = _start_job(
        "load_prediction", req.model_dump(), run_load_prediction, req.service_name
    )
    return {"job_id": job_id}


@app.post("/api/jobs/alert-grouping")
def trigger_alert_grouping():
    from agent.agent_loop import run_alert_grouping

    job_id = _start_job("alert_grouping", {}, run_alert_grouping)
    return {"job_id": job_id}


@app.post("/api/jobs/health-query")
def trigger_health_query(req: HealthQueryRequest):
    from agent.agent_loop import run_health_query

    job_id = _start_job(
        "health_query", req.model_dump(), run_health_query, req.question
    )
    return {"job_id": job_id}


# ── Endpoint — poll a job ───────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    _evict_stale_jobs()
    job = _get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found — it may have expired or never existed.",
        )
    return job


# ── Live service status — for the dashboard's overview page ────────────────

@app.get("/api/status")
def system_status():
    """
    Quick snapshot for the dashboard's landing page: live service health
    plus whether Prometheus is reachable. Reuses the same data sources the
    CLI 'status' command uses, just returned as JSON instead of a Rich table.
    """
    from db.database import get_latest_metric_per_service

    rows = get_latest_metric_per_service()

    try:
        from agent.prometheus_adapter import is_available as prometheus_available
        prom_up = prometheus_available()
    except Exception:
        prom_up = False

    return {
        "prometheus_available": prom_up,
        "services": rows,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_API_PORT", "5050"))
    print(f"Starting SRE Agent Dashboard API on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")