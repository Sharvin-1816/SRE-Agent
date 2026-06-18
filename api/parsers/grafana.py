"""
api/parsers/grafana.py

Parses Grafana Alerting webhook payloads (Grafana 9+ unified alerting format).

Grafana sends a list of alerts under the "alerts" key.
Each alert has labels, annotations, status, and optional values.

Example payload shape:
{
  "receiver": "sre-agent",
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "HighLatency",
        "service": "payment_service",
        "severity": "critical"
      },
      "annotations": {
        "summary": "p95 latency is 3200ms",
        "description": "Threshold breached: 2000ms"
      },
      "startsAt": "2026-06-04T10:00:00Z",
      "values": {"B": 3200}
    }
  ]
}
"""

from api.parsers.normaliser import make_internal_alert


# Labels Grafana might use to identify a service
_SERVICE_LABEL_CANDIDATES = [
    "service", "service_name", "job", "app",
    "application", "instance", "namespace",
]

# Labels Grafana might use for severity
_SEVERITY_LABEL_CANDIDATES = [
    "severity", "priority", "level", "alertseverity",
]

# Grafana's own built-in meta-alerts — these fire when something is wrong
# with ALERTING ITSELF (a datasource stopped returning data, a query
# errored, evaluation timed out), not because any of our 6 mock services
# actually degraded. They carry no `service` label (there's no real
# service to attach one to) and an alertname like "DatasourceNoData" or
# "DatasourceError" — which, without this filter, falls through
# _extract_service()'s alertname-guessing fallback and gets treated as a
# literal service name. normaliser.py's cleanup then appends "_service"
# to whatever's left, producing a fictitious "datasourcenodata_service"
# that the agent ran full RCA/prediction/blast-radius cycles against and
# even stored as a permanent memory pattern — pure noise, plus wasted
# LLM calls, plus polluted long-term memory.
#
# Matched case-insensitively against the alertname label. This list
# covers Grafana's documented built-in alert names; if a new one shows
# up in practice, add it here.
_GRAFANA_META_ALERTNAMES = {
    "datasourcenodata",
    "datasourceerror",
    "datasourceslow",
}


def _is_grafana_meta_alert(labels: dict) -> bool:
    alertname = labels.get("alertname", "").lower()
    return alertname in _GRAFANA_META_ALERTNAMES


def _extract_service(labels: dict) -> str:
    for key in _SERVICE_LABEL_CANDIDATES:
        if key in labels:
            return labels[key]
    # Fall back to alertname if no service label
    alertname = labels.get("alertname", "unknown")
    # Try to extract service from alertname like "HighLatency_payment"
    if "_" in alertname:
        parts = alertname.lower().split("_")
        for part in parts:
            if "service" in part or part in (
                "payment", "cart", "auth", "gateway",
                "inventory", "notification"
            ):
                return part
    return alertname.lower()


def _extract_severity(labels: dict) -> str:
    for key in _SEVERITY_LABEL_CANDIDATES:
        if key in labels:
            return labels[key]
    return "medium"


def _extract_metric(labels: dict, annotations: dict) -> str:
    # Try common label keys first
    for key in ("metric", "alertname", "metric_name"):
        if key in labels:
            return labels[key]
    # Parse from alertname — HighLatency → response_time_ms
    alertname = labels.get("alertname", "").lower()
    metric_hints = {
        "latency":     "response_time_ms",
        "response":    "response_time_ms",
        "error":       "error_rate_pct",
        "cpu":         "cpu_pct",
        "memory":      "memory_pct",
        "throughput":  "throughput_rps",
        "uptime":      "uptime_pct",
    }
    for hint, metric in metric_hints.items():
        if hint in alertname:
            return metric
    return "unknown"


def _extract_value(alert: dict) -> tuple:
    """Extract current value and threshold from a Grafana alert."""
    values    = alert.get("values", {})
    current   = None
    threshold = None

    if values:
        # Grafana stores evaluated values as lettered keys: A, B, C...
        vals = list(values.values())
        if vals:
            try:
                current = float(vals[0])
            except (TypeError, ValueError):
                pass

    # Try to extract threshold from annotations description
    desc = alert.get("annotations", {}).get("description", "")
    if "threshold" in desc.lower():
        import re
        nums = re.findall(r"[\d.]+", desc)
        if len(nums) >= 2:
            try:
                threshold = float(nums[1])
            except (ValueError, IndexError):
                pass

    return current, threshold


def parse(payload: dict) -> list[dict]:
    """
    Parse a Grafana webhook payload.
    Returns a list of normalised internal alert dicts
    (one per alert in the payload).
    """
    alerts      = payload.get("alerts", [])
    status      = payload.get("status", "firing")
    normalised  = []

    for alert in alerts:
        alert_status = alert.get("status", status)

        # Only process firing alerts — skip resolved
        if alert_status.lower() == "resolved":
            continue

        labels      = alert.get("labels", {})
        annotations = alert.get("annotations", {})

        # Skip Grafana's own built-in meta-alerts (DatasourceNoData, etc.)
        # — these are about Grafana's alerting pipeline itself, not about
        # any of our 6 real services. See _GRAFANA_META_ALERTNAMES above
        # for why this matters.
        if _is_grafana_meta_alert(labels):
            continue

        service  = _extract_service(labels)
        severity = _extract_severity(labels)
        metric   = _extract_metric(labels, annotations)
        summary  = (
            annotations.get("summary")
            or annotations.get("message")
            or annotations.get("description")
            or f"Grafana alert: {labels.get('alertname', 'unknown')}"
        )
        current, threshold = _extract_value(alert)

        normalised.append(make_internal_alert(
            source        = "grafana",
            service_name  = service,
            severity      = severity,
            metric        = metric,
            summary       = summary,
            current_value = current,
            threshold     = threshold,
            raw_payload   = alert,
        ))

    return normalised