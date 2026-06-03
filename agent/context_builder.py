"""
agent/context_builder.py

Assembles the full context package sent to the LLM on every agent call.
Pulls from multiple DB tables and formats everything into clean prompt text.

The context package has two forms:
  1. dict  — stored to context_packages table for audit/replay
  2. str   — formatted prompt text injected into the LLM user message
"""

import json
from datetime import datetime, timezone, timedelta
from rich.console import Console
from db.database import (
    get_recent_metrics, get_latest_metric_per_service,
    get_context_as_text, get_ungrouped_alerts,
    get_agent_outputs, insert_context_package,
    get_outputs_for_health_query
)
from config.dependency_map import get_dependency_summary, SERVICE_DESCRIPTIONS

console = Console()

# Try to use Prometheus for richer metrics; fall back to Supabase if unavailable
try:
    from agent.prometheus_adapter import (
        is_available as prometheus_available,
        format_current_metrics_table,
        format_recent_metrics_table,
    )
    _USE_PROMETHEUS = True
except ImportError:
    _USE_PROMETHEUS = False


def _get_metrics_source() -> str:
    """Returns which metrics source is active."""
    if _USE_PROMETHEUS and prometheus_available():
        return "prometheus"
    return "supabase"


def _metrics_source_label() -> str:
    """Returns a label shown in agent prompts indicating data source."""
    source = _get_metrics_source()
    if source == "prometheus":
        return "📡 Live data via Prometheus (p50/p95/p99 latency)"
    return "🗄️ Historical data via Supabase (average latency)"


def _metrics_source_label() -> str:
    """Returns a label string showing which source is active — shown in agent prompts."""
    source = _get_metrics_source()
    if source == "prometheus":
        return "📡 METRICS SOURCE: Prometheus (p50/p95/p99 latency histograms)"
    return "🗄️  METRICS SOURCE: Supabase fallback (average latency only)"


# ── Datetime context ──────────────────────────────────────────────────────────

def _build_datetime_context() -> dict:
    now = datetime.now(timezone.utc)
    days_to_weekend = (5 - now.weekday()) % 7   # days until Saturday

    hour = now.hour
    if   0  <= hour < 6:  tod = "overnight (very low traffic expected)"
    elif 6  <= hour < 12: tod = "morning (traffic ramping up)"
    elif 12 <= hour < 18: tod = "afternoon (peak business hours)"
    else:                 tod = "evening (traffic tapering)"

    return {
        "iso":             now.isoformat(),
        "day_of_week":     now.strftime("%A"),
        "date":            now.strftime("%Y-%m-%d"),
        "time":            now.strftime("%H:%M UTC"),
        "time_of_day":     tod,
        "days_to_weekend": days_to_weekend,
    }


# ── Metrics snapshot ──────────────────────────────────────────────────────────

def _format_recent_metrics(service_name: str) -> str:
    """Last 30 min of metrics for a service as a readable table."""
    source = _get_metrics_source()

    if source == "prometheus":
        console.print(f"  [dim cyan]📡 Using Prometheus for {service_name} metrics[/dim cyan]")
        return format_recent_metrics_table(service_name, minutes=30)

    # Supabase fallback
    console.print(f"  [dim yellow]🗄 Using Supabase fallback for {service_name} metrics[/dim yellow]")
    rows = get_recent_metrics(service_name, minutes=30)
    if not rows:
        return "No recent metrics available."
    lines = ["Time (UTC) | RT (ms) | Error % | Throughput | CPU % | Mem %"]
    lines.append("-" * 65)
    for r in rows[-10:]:
        t   = r["timestamp"][11:16]
        rt  = f"{r.get('response_time_ms', '?'):.0f}"
        er  = f"{r.get('error_rate_pct',   '?'):.1f}"
        tp  = f"{r.get('throughput_rps',   '?'):.0f}"
        cpu = f"{r.get('cpu_pct',          '?'):.1f}"
        mem = f"{r.get('memory_pct',       '?'):.1f}"
        lines.append(f"{t}      | {rt:>7} | {er:>7} | {tp:>10} | {cpu:>5} | {mem:>5}")
    return "\n".join(lines)


def _format_all_latest() -> str:
    """Current status for every service."""
    source = _get_metrics_source()

    if source == "prometheus":
        console.print(f"  [dim cyan]📡 Using Prometheus for live metrics[/dim cyan]")
        return format_current_metrics_table()

    # Supabase fallback
    console.print(f"  [dim yellow]🗄 Using Supabase fallback for live metrics[/dim yellow]")
    rows = get_latest_metric_per_service()
    if not rows:
        return "No metrics available."
    lines = ["Service               | Status | RT (ms) | Error % | Throughput"]
    lines.append("-" * 70)
    for r in rows:
        status = "UP  " if r.get("is_reachable") else "DOWN"
        svc    = r["service_name"][:22].ljust(22)
        rt     = f"{r.get('response_time_ms', 0):.0f}"
        er     = f"{r.get('error_rate_pct',   0):.1f}"
        tp     = f"{r.get('throughput_rps',   0):.0f}"
        lines.append(f"{svc} | {status} | {rt:>7} | {er:>7} | {tp:>10}")
    return "\n".join(lines)


# ── Alert summary ─────────────────────────────────────────────────────────────

def _format_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return "No active alerts."
    lines = []
    for a in alerts:
        lines.append(
            f"  [{a['severity'].upper()}] {a['service_name']} — "
            f"{a['metric']}: {a['message']}"
        )
    return "\n".join(lines)


# ── Health query data ─────────────────────────────────────────────────────────

def _parse_time_range_from_query(question: str) -> tuple[str, str]:
    """
    Extract time range from NL query.
    Handles: "this weekend", "last weekend", "yesterday",
             "last 24 hours", "this week" etc.
    """
    now   = datetime.now(timezone.utc)
    lower = question.lower()

    if "last weekend" in lower or "this weekend" in lower or "weekend" in lower:
        # Find last Saturday/Sunday
        days_since_sat = (now.weekday() - 5) % 7
        sat_start = (now - timedelta(days=days_since_sat)).replace(
            hour=0, minute=0, second=0)
        sun_end   = sat_start + timedelta(days=2)
        return sat_start.isoformat(), min(sun_end, now).isoformat()

    elif "yesterday" in lower:
        yesterday = now - timedelta(days=1)
        start = yesterday.replace(hour=0,  minute=0,  second=0)
        end   = yesterday.replace(hour=23, minute=59, second=59)
        return start.isoformat(), end.isoformat()

    elif "last hour" in lower:
        return (now - timedelta(hours=1)).isoformat(), now.isoformat()

    elif "last 24" in lower or "today" in lower:
        return (now - timedelta(hours=24)).isoformat(), now.isoformat()

    elif "last week" in lower or "this week" in lower:
        return (now - timedelta(days=7)).isoformat(), now.isoformat()

    else:
        # Default: last 24 hours
        return (now - timedelta(hours=24)).isoformat(), now.isoformat()


# ── Main builders ─────────────────────────────────────────────────────────────

def build_for_rca(service_name: str, signal: dict) -> tuple[dict, str]:
    """Build context package and prompt text for RCA mode."""
    dt       = _build_datetime_context()
    ctx_text = get_context_as_text()
    metrics  = _format_recent_metrics(service_name)
    alerts   = _format_alerts(get_ungrouped_alerts(window_minutes=10))

    package = {
        "trigger_reason": "anomaly_detected",
        "llm_signal":     signal,
        "recent_metrics": metrics,
        "datetime_ctx":   dt,
        "context_store":  ctx_text,
    }
    saved = insert_context_package(package)

    prompt = f"""
ANOMALY SIGNAL FOR {service_name.upper().replace('_', ' ')}
METRICS SOURCE: {_metrics_source_label()}
═══════════════════════════════════════════════════════════════
{signal.get('human_summary', 'No summary available.')}

ANOMALOUS METRICS:
{json.dumps(signal.get('metrics_snapshot', {}), indent=2)}

TREND ANALYSIS:
{signal.get('trend_summary', 'No trend data.')}

HYPOTHESIS HINTS FROM ANOMALY ENGINE:
{chr(10).join('• ' + h for h in signal.get('hypothesis_hints', []))}

CORRELATED SERVICES (also degrading right now):
{json.dumps(signal.get('correlated_services', []), indent=2)}

RECENT METRICS (last 30 min):
{metrics}

ACTIVE ALERTS:
{alerts}

CURRENT TIME CONTEXT:
{dt['day_of_week']}, {dt['date']} at {dt['time']} — {dt['time_of_day']}

OPERATOR CONTEXT (provided by user — treat as ground truth):
─────────────────────────────────────────────────────────────
{ctx_text}
─────────────────────────────────────────────────────────────

Perform root cause analysis. Return JSON only.
"""
    return saved, prompt


def build_for_prediction(service_name: str, signal: dict) -> tuple[dict, str]:
    """Build context package and prompt text for degradation prediction."""
    dt       = _build_datetime_context()
    ctx_text = get_context_as_text()
    metrics  = _format_recent_metrics(service_name)

    package = {
        "trigger_reason": "anomaly_detected",
        "llm_signal":     signal,
        "recent_metrics": metrics,
        "datetime_ctx":   dt,
        "context_store":  ctx_text,
    }
    saved = insert_context_package(package)

    prompt = f"""
DEGRADATION PREDICTION REQUEST FOR {service_name.upper().replace('_', ' ')}
═══════════════════════════════════════════════════════════════
CURRENT ANOMALY SIGNAL:
{signal.get('human_summary', '')}

TREND DATA:
{signal.get('trend_summary', 'No trend data.')}

RECENT METRICS (last 30 min, showing trajectory):
{metrics}

CURRENT TIME CONTEXT:
{dt['day_of_week']}, {dt['date']} at {dt['time']} — {dt['time_of_day']}
Days until weekend: {dt['days_to_weekend']}

OPERATOR CONTEXT (provided by user — treat as ground truth):
─────────────────────────────────────────────────────────────
{ctx_text}
─────────────────────────────────────────────────────────────
NOTE: If any operator context mentions upcoming events (sales, launches,
outages, deployments), factor those into your failure timeline prediction.

Predict degradation trajectory. Return JSON only.
"""
    return saved, prompt


def build_for_load_prediction(service_name: str = None) -> tuple[dict, str]:
    """Build context for load prediction — system-wide view."""
    dt        = _build_datetime_context()
    ctx_text  = get_context_as_text()
    all_svcs  = _format_all_latest()

    package = {
        "trigger_reason": "load_prediction",
        "datetime_ctx":   dt,
        "context_store":  ctx_text,
        "all_services":   all_svcs,
    }
    saved = insert_context_package(package)

    focus = f"Focus on: {service_name}" if service_name else "Analyse all services."

    prompt = f"""
LOAD PREDICTION REQUEST
═══════════════════════════════════════════════════════════════
{focus}

CURRENT SERVICE STATUS (all services):
{all_svcs}

CURRENT TIME CONTEXT:
{dt['day_of_week']}, {dt['date']} at {dt['time']} — {dt['time_of_day']}
Days until weekend: {dt['days_to_weekend']}

OPERATOR CONTEXT (provided by user — treat as ground truth):
─────────────────────────────────────────────────────────────
{ctx_text}
─────────────────────────────────────────────────────────────
IMPORTANT: Use the operator context to identify upcoming events that will
cause load spikes. A movie release, flash sale, power outage, or deployment
all directly affect load predictions. Be specific about which events you are
using and how they change your forecast.

Predict future load and capacity needs. Return JSON only.
"""
    return saved, prompt


def build_for_alert_grouping(alerts: list[dict]) -> tuple[dict, str]:
    """Build context for alert noise reduction."""
    dt       = _build_datetime_context()
    ctx_text = get_context_as_text()

    # Build a short-ID → full-ID lookup so the LLM uses short IDs in its
    # response but we can resolve back to full UUIDs when saving to DB.
    id_map = {a['id'][:8]: a['id'] for a in alerts}

    formatted_alerts = []
    for a in alerts:
        formatted_alerts.append(
            f"  ID={a['id'][:8]} | [{a['severity'].upper()}] "
            f"{a['service_name']} | metric={a['metric']} | {a['message']}"
        )

    package = {
        "trigger_reason": "alert_grouping",
        "datetime_ctx":   dt,
        "context_store":  ctx_text,
    }
    saved = insert_context_package(package)

    prompt = f"""
ALERT NOISE REDUCTION REQUEST
═══════════════════════════════════════════════════════════════
{len(alerts)} ACTIVE ALERTS FIRING RIGHT NOW:
{chr(10).join(formatted_alerts)}

IMPORTANT: Use the short IDs (e.g. "d93ef320") exactly as shown above
in the alert_ids_grouped field of your response.

CURRENT TIME: {dt['day_of_week']} {dt['time']} — {dt['time_of_day']}

OPERATOR CONTEXT:
─────────────────────────────────────────────────────────────
{ctx_text}
─────────────────────────────────────────────────────────────

Group these alerts into meaningful incidents. Suppress redundant noise.
Identify the single root cause driving multiple alerts.
Return JSON only.
"""
    # Attach id_map to saved package so agent_loop can resolve full UUIDs
    saved["_id_map"] = id_map
    return saved, prompt


def build_for_health_query(question: str) -> tuple[dict, str]:
    """Build context for natural language health query."""
    dt       = _build_datetime_context()
    ctx_text = get_context_as_text()

    since, until = _parse_time_range_from_query(question)

    # Get historical agent outputs for that time range
    past_outputs = get_outputs_for_health_query(since, until)
    past_text    = "No agent analysis records for this time period."
    if past_outputs:
        lines = []
        for o in past_outputs[:10]:
            rca = o.get("rca") or {}
            lines.append(
                f"  [{o['generated_at'][:16]}] {o['service_name']} — "
                f"{rca.get('root_cause', 'analysis recorded')}"
            )
        past_text = "\n".join(lines)

    # Full live metrics — CPU, memory, throughput, error rate all included
    # This prevents the agent from asking the user for data it already has
    all_svcs = _format_all_latest()

    # Also pull latest raw metrics for richer per-service detail
    latest_rows = get_latest_metric_per_service()
    live_detail_lines = ["Service               | CPU % | Mem % | RT(ms) | Error% | Throughput | Status"]
    live_detail_lines.append("-" * 85)
    for r in latest_rows:
        svc  = r["service_name"][:22].ljust(22)
        cpu  = f"{r.get('cpu_pct', 0):.1f}"
        mem  = f"{r.get('memory_pct', 0):.1f}"
        rt   = f"{r.get('response_time_ms', 0):.0f}"
        er   = f"{r.get('error_rate_pct', 0):.1f}"
        tp   = f"{r.get('throughput_rps', 0):.0f}"
        up   = "UP" if r.get("is_reachable", True) else "DOWN"
        live_detail_lines.append(f"{svc} | {cpu:>5} | {mem:>5} | {rt:>6} | {er:>6} | {tp:>10} | {up}")
    live_detail = "\n".join(live_detail_lines)

    package = {
        "trigger_reason": "health_query",
        "question":       question,
        "time_range":     {"since": since, "until": until},
        "datetime_ctx":   dt,
        "context_store":  ctx_text,
    }
    saved = insert_context_package(package)

    prompt = f"""
HEALTH QUERY REQUEST
═══════════════════════════════════════════════════════════════
USER QUESTION: "{question}"
METRICS SOURCE: {_metrics_source_label()}

QUERIED TIME RANGE: {since[:16]} to {until[:16]} UTC

LIVE SERVICE METRICS RIGHT NOW (use this to answer current-state questions):
{live_detail}

PAST AGENT ANALYSES IN THIS PERIOD:
{past_text}

CURRENT TIME: {dt['day_of_week']}, {dt['date']} at {dt['time']} — {dt['time_of_day']}

OPERATOR CONTEXT:
─────────────────────────────────────────────────────────────
{ctx_text}
─────────────────────────────────────────────────────────────

IMPORTANT: All live metrics are provided above. Do NOT ask the user for
CPU, memory, throughput, or response time data — it is already here.
Answer the user's question directly using the data above. Return JSON only.
"""
    return saved, prompt


def build_for_blast_radius(service_name: str, signal: dict = None) -> tuple[dict, str]:
    """Build context for blast radius estimation."""
    dt           = _build_datetime_context()
    ctx_text     = get_context_as_text()
    dep_summary  = get_dependency_summary(service_name)
    all_svcs     = _format_all_latest()
    svc_desc     = SERVICE_DESCRIPTIONS.get(service_name, "")

    package = {
        "trigger_reason": "blast_radius",
        "service_name":   service_name,
        "llm_signal":     signal,
        "datetime_ctx":   dt,
        "context_store":  ctx_text,
    }
    saved = insert_context_package(package)

    signal_text = signal.get("human_summary", "Service is degrading.") if signal else \
                  f"{service_name} has been manually flagged for blast radius analysis."

    prompt = f"""
BLAST RADIUS ESTIMATION FOR {service_name.upper().replace('_', ' ')}
═══════════════════════════════════════════════════════════════
FAILING SERVICE: {service_name}
DESCRIPTION: {svc_desc}

WHAT IS HAPPENING:
{signal_text}

DEPENDENCY MAP (base failure probabilities):
{dep_summary}

ALL SERVICES CURRENT STATUS:
{all_svcs}

CURRENT TIME: {dt['day_of_week']} {dt['time']} — {dt['time_of_day']}

OPERATOR CONTEXT:
─────────────────────────────────────────────────────────────
{ctx_text}
─────────────────────────────────────────────────────────────

Estimate blast radius. Adjust base probabilities based on current service
health and operator context. Return JSON only.
"""
    return saved, prompt