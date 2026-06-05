"""
api/decisions_api.py

Lightweight FastAPI app that exposes agent decisions from Supabase
in a format Grafana's JSON datasource plugin can consume.

Grafana JSON datasource expects three endpoints:
  GET  /               → health check
  POST /search         → list available metrics/series
  POST /query          → return time-series or table data
  POST /annotations    → return annotations (events on timeline)

Run standalone:
  python -m api.decisions_api

Runs on port 5000 by default.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn
from dotenv import load_dotenv

load_dotenv()

from db.database import get_agent_outputs, db

app = FastAPI(title="SRE Agent Decisions API")

# Grafana needs CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request models (Grafana JSON datasource format) ───────────────────────────

class TimeRange(BaseModel):
    from_: Optional[str] = None
    to: Optional[str] = None

    model_config = {"populate_by_name": True}

    def __init__(self, **data):
        if "from" in data:
            data["from_"] = data.pop("from")
        super().__init__(**data)


class QueryTarget(BaseModel):
    target: Optional[str] = "decisions"
    type: Optional[str] = "table"


class QueryRequest(BaseModel):
    range: Optional[dict] = None
    targets: Optional[list] = []
    maxDataPoints: Optional[int] = 100


class AnnotationRequest(BaseModel):
    range: Optional[dict] = None
    annotation: Optional[dict] = None


# ── Severity color mapping ────────────────────────────────────────────────────

SEVERITY_COLORS = {
    "critical": "#F2495C",
    "high":     "#FF9830",
    "medium":   "#FADE2A",
    "low":      "#73BF69",
}

MODE_ICONS = {
    "rca":                "⚠",
    "predict_degradation": "📈",
    "load_prediction":    "📊",
    "alert_grouping":     "🔔",
    "health_query":       "💬",
    "blast_radius":       "💥",
}


# ── Helper functions ──────────────────────────────────────────────────────────

def _parse_grafana_time(ts: str) -> datetime:
    """Parse Grafana ISO timestamp to datetime."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=24)


def _to_ms(dt: datetime) -> int:
    """Convert datetime to milliseconds timestamp for Grafana."""
    return int(dt.timestamp() * 1000)


def _extract_summary(output: dict) -> str:
    """Extract a one-line summary from an agent output."""
    mode = output.get("mode", "")

    if mode == "rca":
        rca = output.get("rca") or {}
        return rca.get("root_cause", "RCA completed")

    elif mode == "predict_degradation":
        pred = output.get("prediction") or {}
        will_fail = pred.get("will_fail", False)
        ttf = pred.get("estimated_time_to_failure", "unknown")
        return f"Will fail: {'YES' if will_fail else 'NO'} — {ttf}"

    elif mode == "load_prediction":
        load = output.get("load_prediction") or {}
        mult = load.get("expected_load_multiplier", "?")
        return f"Expected load: {mult}x normal"

    elif mode == "alert_grouping":
        grp = output.get("alert_group") or {}
        total_in  = grp.get("total_alerts_in", "?")
        total_out = grp.get("total_incidents_out", "?")
        noise     = grp.get("noise_reduction_pct", "?")
        return f"{total_in} alerts → {total_out} incidents ({noise}% noise reduced)"

    elif mode == "health_query":
        rca = output.get("rca") or {}
        return rca.get("answer", "Health query completed")

    elif mode == "blast_radius":
        br = output.get("blast_radius") or {}
        chain = br.get("impact_chain", [])
        if chain:
            top = chain[0]
            return f"{top.get('service','?')} at {top.get('failure_probability_pct','?')}% risk"
        return "Blast radius estimated"

    return f"{mode} analysis completed"


def _extract_severity(output: dict) -> str:
    """Extract severity from agent output."""
    rca = output.get("rca") or {}
    if isinstance(rca, dict):
        # Check fix suggestions count as proxy for severity
        fixes = rca.get("fix_suggestions", [])
        if len(fixes) >= 5:
            return "critical"
        elif len(fixes) >= 3:
            return "high"

    confidence = output.get("confidence", 50)
    if confidence and confidence >= 85:
        return "high"
    elif confidence and confidence >= 70:
        return "medium"
    return "low"


def _get_outputs_in_range(since: datetime, until: datetime) -> list[dict]:
    """Fetch agent outputs within a time range."""
    hours = max(1, int((until - since).total_seconds() / 3600) + 1)
    return get_agent_outputs(since_hours=hours)


# ── Grafana JSON datasource endpoints ────────────────────────────────────────

@app.get("/")
def health():
    """Grafana health check."""
    return {"status": "ok", "service": "SRE Agent Decisions API"}


@app.post("/search")
def search():
    """Return available query targets."""
    return [
        "decisions",
        "decisions_by_service",
        "decisions_by_mode",
        "confidence_over_time",
    ]


@app.post("/query")
def query(req: QueryRequest):
    """
    Return agent decisions as Grafana table or time-series data.
    """
    now   = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    until = now

    if req.range:
        since = _parse_grafana_time(req.range.get("from", ""))
        until = _parse_grafana_time(req.range.get("to", ""))

    outputs = _get_outputs_in_range(since, until)

    results = []
    for target in (req.targets or [{"target": "decisions"}]):
        t = target.get("target", "decisions") if isinstance(target, dict) else "decisions"

        if t == "decisions":
            # Table format — all decisions with details
            rows = []
            for o in outputs:
                ts = _parse_grafana_time(o.get("generated_at", ""))
                if not (since <= ts <= until):
                    continue
                rows.append([
                    _to_ms(ts),
                    o.get("service_name") or "system",
                    o.get("mode", "").replace("_", " ").title(),
                    o.get("confidence", 0),
                    _extract_summary(o),
                    _extract_severity(o).upper(),
                    "YES" if o.get("needed_more_context") else "NO",
                ])

            results.append({
                "columns": [
                    {"text": "Time",            "type": "time"},
                    {"text": "Service",         "type": "string"},
                    {"text": "Mode",            "type": "string"},
                    {"text": "Confidence",      "type": "number"},
                    {"text": "Summary",         "type": "string"},
                    {"text": "Severity",        "type": "string"},
                    {"text": "Asked User",      "type": "string"},
                ],
                "rows": rows,
                "type": "table",
            })

        elif t == "confidence_over_time":
            # Time-series of confidence scores per mode
            series = {}
            for o in outputs:
                ts = _parse_grafana_time(o.get("generated_at", ""))
                if not (since <= ts <= until):
                    continue
                mode = o.get("mode", "unknown")
                conf = o.get("confidence", 0) or 0
                if mode not in series:
                    series[mode] = []
                series[mode].append([conf, _to_ms(ts)])

            for mode, datapoints in series.items():
                results.append({
                    "target": mode.replace("_", " ").title(),
                    "datapoints": sorted(datapoints, key=lambda x: x[1]),
                })

        elif t == "decisions_by_service":
            # Count of decisions per service
            counts = {}
            for o in outputs:
                ts = _parse_grafana_time(o.get("generated_at", ""))
                if not (since <= ts <= until):
                    continue
                svc = o.get("service_name") or "system"
                counts[svc] = counts.get(svc, 0) + 1

            rows = [[svc, count] for svc, count in sorted(counts.items(), key=lambda x: -x[1])]
            results.append({
                "columns": [
                    {"text": "Service", "type": "string"},
                    {"text": "Decisions", "type": "number"},
                ],
                "rows": rows,
                "type": "table",
            })

        elif t == "decisions_by_mode":
            counts = {}
            for o in outputs:
                ts = _parse_grafana_time(o.get("generated_at", ""))
                if not (since <= ts <= until):
                    continue
                mode = o.get("mode", "unknown").replace("_", " ").title()
                counts[mode] = counts.get(mode, 0) + 1

            rows = [[mode, count] for mode, count in sorted(counts.items(), key=lambda x: -x[1])]
            results.append({
                "columns": [
                    {"text": "Mode",      "type": "string"},
                    {"text": "Count",     "type": "number"},
                ],
                "rows": rows,
                "type": "table",
            })

    return results


@app.post("/annotations")
def annotations(req: AnnotationRequest):
    """
    Return agent decisions as Grafana timeline annotations.
    These appear as vertical lines on existing panels showing
    exactly when the agent fired and what it concluded.
    """
    now   = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    until = now

    if req.range:
        since = _parse_grafana_time(req.range.get("from", ""))
        until = _parse_grafana_time(req.range.get("to", ""))

    outputs = _get_outputs_in_range(since, until)

    result = []
    for o in outputs:
        ts = _parse_grafana_time(o.get("generated_at", ""))
        if not (since <= ts <= until):
            continue

        mode     = o.get("mode", "unknown")
        service  = o.get("service_name") or "system"
        icon     = MODE_ICONS.get(mode, "🤖")
        severity = _extract_severity(o)
        summary  = _extract_summary(o)
        color    = SEVERITY_COLORS.get(severity, "#73BF69")
        conf     = o.get("confidence", 0) or 0

        result.append({
            "annotation": req.annotation,
            "time":       _to_ms(ts),
            "title":      f"{icon} {mode.replace('_', ' ').title()} — {service}",
            "text":       f"{summary} (confidence: {conf}%)",
            "tags":       [service, mode, severity],
            "color":      color,
        })

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("DECISIONS_API_PORT", "5000"))
    print(f"Starting SRE Agent Decisions API on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")