"""
api/parsers/normaliser.py

Converts payloads from Grafana, PagerDuty, and Datadog into a single
internal alert format. Everything downstream only ever sees this format.
"""

from datetime import datetime, timezone


# ── Internal format ───────────────────────────────────────────────────────────

def make_internal_alert(
    source:        str,
    service_name:  str,
    severity:      str,
    metric:        str,
    summary:       str,
    current_value: float = None,
    threshold:     float = None,
    raw_payload:   dict  = None,
) -> dict:
    """
    Build a normalised internal alert dict.
    This is the only format the agent and DB layer ever receive.
    """
    # Normalise severity to our four levels
    sev_map = {
        "critical": "critical",
        "error":    "critical",
        "high":     "high",
        "warning":  "high",
        "medium":   "medium",
        "info":     "low",
        "low":      "low",
        "ok":       "low",
        "resolved": "low",
    }
    normalised_sev = sev_map.get(severity.lower(), "medium")

    # Best-effort service name cleanup
    svc = service_name.lower().strip()
    svc = svc.replace("-", "_").replace(" ", "_")
    if not svc.endswith("_service") and "_service" not in svc:
        svc = f"{svc}_service"

    return {
        "source":        source,
        "service_name":  svc,
        "severity":      normalised_sev,
        "metric":        metric or "unknown",
        "summary":       summary or f"Alert from {source}",
        "current_value": current_value,
        "threshold":     threshold,
        "raw_payload":   raw_payload or {},
        "received_at":   datetime.now(timezone.utc).isoformat(),
    }
