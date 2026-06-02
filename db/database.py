"""
db/database.py
Supabase client + all read/write operations for every table.
Every other module imports from here — nothing else touches Supabase directly.
"""

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ── Client ────────────────────────────────────────────────────────────────────

def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)

_client: Optional[Client] = None

def db() -> Client:
    global _client
    if _client is None:
        _client = get_client()
    return _client


# ── 1. metrics_raw ────────────────────────────────────────────────────────────

def insert_metric(row: dict) -> dict:
    """
    Write one raw metric row from the collector.

    Expected keys:
        service_name, response_time_ms, error_rate_pct, throughput_rps,
        uptime_pct, upload_time_ms, cpu_pct, memory_pct,
        status_code, is_reachable, error_message (optional)
    """
    result = db().table("metrics_raw").insert(row).execute()
    return result.data[0]


def get_recent_metrics(service_name: str, minutes: int = 30) -> list[dict]:
    """
    Fetch all rows for a service in the last N minutes.
    Used by context builder for the short-window snapshot.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    result = (
        db().table("metrics_raw")
        .select("*")
        .eq("service_name", service_name)
        .gte("timestamp", since)
        .order("timestamp", desc=False)
        .execute()
    )
    return result.data


def get_historical_window(
    service_name: str,
    hour_start: int,
    hour_end: int,
    days_back: int = 7
) -> list[dict]:
    """
    Fetch rows for the same time-of-day window over the last N days.
    Used by the baseline engine and context builder for 7-day history.

    Example: get all readings between 12:00–17:00 for the last 7 days.
    Uses Postgres EXTRACT via RPC — see rpc/get_historical_window.sql
    """
    result = db().rpc(
        "get_historical_window",
        {
            "p_service": service_name,
            "p_hour_start": hour_start,
            "p_hour_end": hour_end,
            "p_days_back": days_back,
        }
    ).execute()
    return result.data


def get_latest_metric_per_service() -> list[dict]:
    """Returns the most recent reading for every service (uses the view)."""
    result = db().table("latest_metrics").select("*").execute()
    return result.data


# ── 2. baseline_profiles ──────────────────────────────────────────────────────

def upsert_baseline(profile: dict) -> dict:
    """
    Insert or update a baseline profile for a service + time_window.

    Expected keys:
        service_name, time_window, hour_start, hour_end, sample_count,
        rt_mean, rt_std, er_mean, er_std, tp_mean, tp_std,
        cpu_mean, cpu_std, mem_mean, mem_std
    """
    profile["last_updated"] = datetime.now(timezone.utc).isoformat()
    result = (
        db().table("baseline_profiles")
        .upsert(profile, on_conflict="service_name,time_window")
        .execute()
    )
    return result.data[0]


def get_baseline(service_name: str, time_window: str) -> Optional[dict]:
    """
    Fetch the baseline profile for a specific service + time window.
    Returns None if not enough data yet.
    """
    result = (
        db().table("baseline_profiles")
        .select("*")
        .eq("service_name", service_name)
        .eq("time_window", time_window)
        .execute()
    )
    rows = result.data
    if not rows:
        return None
    profile = rows[0]
    min_samples = int(os.getenv("BASELINE_MIN_SAMPLES", "50"))
    if profile["sample_count"] < min_samples:
        return None  # not enough data yet — caller should use fallback thresholds
    return profile


def get_all_baselines(service_name: str) -> list[dict]:
    """Fetch all time-window baselines for a service."""
    result = (
        db().table("baseline_profiles")
        .select("*")
        .eq("service_name", service_name)
        .execute()
    )
    return result.data


# ── 3. anomaly_events ─────────────────────────────────────────────────────────

def insert_anomaly(event: dict) -> dict:
    """
    Write a new anomaly event.

    Expected keys:
        service_name, max_z_score, severity,
        anomalies (list), trend (dict), correlated_services (list)
    """
    result = db().table("anomaly_events").insert(event).execute()
    return result.data[0]


def get_unprocessed_anomalies() -> list[dict]:
    """Fetch anomalies not yet sent to the agent."""
    result = (
        db().table("anomaly_events")
        .select("*")
        .eq("processed_by_agent", False)
        .order("detected_at", desc=False)
        .execute()
    )
    return result.data


def mark_anomaly_processed(anomaly_id: str) -> None:
    db().table("anomaly_events").update(
        {"processed_by_agent": True}
    ).eq("id", anomaly_id).execute()


def get_recent_anomalies(service_name: str, hours: int = 24) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    result = (
        db().table("anomaly_events")
        .select("*")
        .eq("service_name", service_name)
        .gte("detected_at", since)
        .order("detected_at", desc=True)
        .execute()
    )
    return result.data


# ── 4. llm_signals ────────────────────────────────────────────────────────────

def insert_llm_signal(signal: dict) -> dict:
    """
    Write a formatted LLM-ready signal.

    Expected keys:
        anomaly_event_id, service_name, severity,
        human_summary, metrics_snapshot, trend_summary,
        context_window, hypothesis_hints, correlated_services
    """
    result = db().table("llm_signals").insert(signal).execute()
    return result.data[0]


def get_latest_signal(service_name: str) -> Optional[dict]:
    result = (
        db().table("llm_signals")
        .select("*")
        .eq("service_name", service_name)
        .order("generated_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ── 5. context_packages ───────────────────────────────────────────────────────

def insert_context_package(package: dict) -> dict:
    """
    Store the full context bundle sent to the agent.
    Returns the inserted row (with id) for linking to agent_outputs.
    """
    result = db().table("context_packages").insert(package).execute()
    return result.data[0]


def get_context_packages(
    trigger_reason: Optional[str] = None,
    limit: int = 20
) -> list[dict]:
    q = db().table("context_packages").select("*")
    if trigger_reason:
        q = q.eq("trigger_reason", trigger_reason)
    result = q.order("created_at", desc=True).limit(limit).execute()
    return result.data


# ── 6. agent_outputs ──────────────────────────────────────────────────────────

def insert_agent_output(output: dict) -> dict:
    """
    Store the agent's structured response.

    Expected keys:
        context_package_id, mode, service_name, confidence,
        needed_more_context, context_question, user_answer,
        rca, prediction, load_prediction, blast_radius,
        alert_group, fix_suggestions, raw_llm_response
    """
    result = db().table("agent_outputs").insert(output).execute()
    return result.data[0]


def get_agent_outputs(
    service_name: Optional[str] = None,
    mode: Optional[str] = None,
    since_hours: int = 24
) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    q = (
        db().table("agent_outputs")
        .select("*")
        .gte("generated_at", since)
        .order("generated_at", desc=True)
    )
    if service_name:
        q = q.eq("service_name", service_name)
    if mode:
        q = q.eq("mode", mode)
    result = q.execute()
    return result.data


def get_outputs_for_health_query(
    since: str,
    until: str,
    service_name: Optional[str] = None
) -> list[dict]:
    """
    Used by the NL health query module to look up past outputs
    in a specific time range.
    """
    q = (
        db().table("agent_outputs")
        .select("service_name, mode, confidence, rca, prediction, generated_at")
        .gte("generated_at", since)
        .lte("generated_at", until)
        .order("generated_at", desc=True)
    )
    if service_name:
        q = q.eq("service_name", service_name)
    return q.execute().data


# ── 7. context_store ──────────────────────────────────────────────────────────

def add_user_context(text: str, source: str = "user_provided") -> dict:
    """
    Add a free-text context entry exactly as the user typed it.

    The key is auto-generated from the timestamp so the user never
    has to think about keys — they just type plain English.

    Examples:
        add_user_context("There will be a power down on 3rd March")
        add_user_context("New movie releasing 9th April, big traffic expected")
        add_user_context("Flash sale every Friday 6-9 PM")
        add_user_context("Deployment v3.0 tonight at 11 PM")
    """
    now = datetime.now(timezone.utc)
    key = f"ctx_{now.strftime('%Y%m%d_%H%M%S_%f')}"
    row = {
        "key":        key,
        "value":      text.strip(),
        "source":     source,
        "is_active":  True,
        "added_at":   now.isoformat(),
        "expires_at": None,
    }
    result = db().table("context_store").insert(row).execute()
    return result.data[0]


def get_active_context() -> list[dict]:
    """
    Returns all active context entries ordered by most recent first.
    Called by context builder on every agent invocation.
    The agent receives these as a plain-text block and reasons freely.
    """
    result = (
        db().table("context_store")
        .select("value, source, added_at")
        .eq("is_active", True)
        .order("added_at", desc=False)   # chronological — older context first
        .execute()
    )
    return result.data


def get_context_as_text() -> str:
    """
    Returns all active context formatted as a single text block
    ready to be injected directly into the LLM prompt.

    Output example:
        [2024-03-01] There will be a power down on 3rd March
        [2024-03-15] New movie release on 9th April, big traffic expected
        [2024-03-20] Flash sale runs every Friday evening 6-9 PM
    """
    entries = get_active_context()
    if not entries:
        return "No operator context provided."

    lines = []
    for e in entries:
        date_str = e["added_at"][:10]   # just YYYY-MM-DD
        lines.append(f"[{date_str}] {e['value']}")
    return "\n".join(lines)


def delete_context_entry(key: str) -> None:
    """Soft-delete a context entry by key."""
    db().table("context_store").update({"is_active": False}).eq("key", key).execute()


def list_context_entries() -> list[dict]:
    """
    Returns all active entries with their keys — used by the CLI
    to let the user see and delete specific entries.
    """
    result = (
        db().table("context_store")
        .select("id, key, value, source, added_at")
        .eq("is_active", True)
        .order("added_at", desc=False)
        .execute()
    )
    return result.data


# ── 8. alerts ─────────────────────────────────────────────────────────────────

def insert_alert(alert: dict) -> dict:
    """
    Expected keys: service_name, anomaly_id, metric, severity, message
    """
    result = db().table("alerts").insert(alert).execute()
    return result.data[0]


def get_ungrouped_alerts(window_minutes: int = 5) -> list[dict]:
    """Fetch recent ungrouped alerts for noise reduction processing."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    result = (
        db().table("alerts")
        .select("*")
        .eq("is_grouped", False)
        .gte("triggered_at", since)
        .order("triggered_at", desc=False)
        .execute()
    )
    return result.data


def mark_alerts_grouped(alert_ids: list[str], incident_id: str) -> None:
    for aid in alert_ids:
        db().table("alerts").update(
            {"is_grouped": True, "incident_id": incident_id}
        ).eq("id", aid).execute()


# ── 9. incidents ──────────────────────────────────────────────────────────────

def insert_incident(incident: dict) -> dict:
    """
    Expected keys:
        title, affected_services (list), raw_alert_count,
        suppressed_count, agent_output_id (optional)
    """
    result = db().table("incidents").insert(incident).execute()
    return result.data[0]


def get_open_incidents() -> list[dict]:
    result = (
        db().table("incidents")
        .select("*")
        .eq("status", "open")
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


def resolve_incident(incident_id: str) -> None:
    db().table("incidents").update({
        "status": "resolved",
        "resolved_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", incident_id).execute()