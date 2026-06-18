"""
api/parsers/normaliser.py

Converts payloads from Grafana, PagerDuty, and Datadog into a single
internal alert format. Everything downstream only ever sees this format.
"""

from datetime import datetime, timezone

# The project's 6 real mock services. Intentionally duplicated here
# rather than imported from services.definitions.ALL_SERVICES — that
# module instantiates 6 live BaseService objects with background
# threads as a side effect of being imported, which is far too heavy a
# dependency for a lightweight validation check in a parser module, and
# would wrongly couple api/parsers/ to services/.
#
# This list is a second line of defense, not the primary filter — the
# primary filter is grafana.py's _GRAFANA_META_ALERTNAMES, which rejects
# known Grafana system alerts (DatasourceNoData, etc.) before they ever
# reach this function. This check exists for whatever that allow-list
# hasn't been told about yet: a new Grafana meta-alert, a malformed
# PagerDuty/Datadog payload, or any other source producing a
# service_name that isn't actually one of our real services. Rather
# than silently fabricate a fictitious "<garbage>_service" name (which
# is exactly what caused "datasourcenodata_service" to get treated as
# real, including having a permanent memory pattern stored for it),
# anything unrecognized is tagged "unknown_service" — visible and
# obviously wrong if it shows up in the CLI/dashboard/logs, instead of
# silently masquerading as legitimate operational history.
_KNOWN_SERVICES = {
    "payment_service", "cart_service", "notification_service",
    "auth_service", "inventory_service", "gateway_service",
}


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

    # Defense in depth — see _KNOWN_SERVICES comment above.
    if svc not in _KNOWN_SERVICES:
        svc = "unknown_service"

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