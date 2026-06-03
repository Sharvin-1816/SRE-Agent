"""
agent/prometheus_adapter.py

Queries the Prometheus HTTP API and formats results for the agent.
This replaces direct Supabase metric queries in context_builder.py
for real-time data. Supabase is still used for everything else.

If Prometheus is unavailable, falls back to Supabase metrics gracefully.

Prometheus HTTP API docs:
  GET /api/v1/query       → instant query (current value)
  GET /api/v1/query_range → range query (time series)
"""

import os
import httpx
from datetime import datetime, timezone, timedelta
from rich.console import Console

console = Console()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

SERVICES = [
    "payment_service",
    "cart_service",
    "notification_service",
    "auth_service",
    "inventory_service",
    "gateway_service",
]

# Port → service name mapping (matches service_runner.py)
PORT_TO_SERVICE = {
    "3001": "payment_service",
    "3002": "cart_service",
    "3003": "notification_service",
    "3004": "auth_service",
    "3005": "inventory_service",
    "3006": "gateway_service",
}


# ── Low-level Prometheus query helpers ───────────────────────────────────────

def _instant_query(promql: str) -> list[dict]:
    """Run an instant PromQL query. Returns list of {metric, value} dicts."""
    try:
        resp = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
        return []
    except Exception:
        return []


def _range_query(promql: str, minutes: int = 30) -> list[dict]:
    """Run a range PromQL query over the last N minutes."""
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=minutes)).timestamp()
    end   = now.timestamp()
    try:
        resp = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={
                "query": promql,
                "start": start,
                "end":   end,
                "step":  "15s",
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
        return []
    except Exception:
        return []


def is_available() -> bool:
    """Check if Prometheus is reachable."""
    try:
        resp = httpx.get(f"{PROMETHEUS_URL}/-/healthy", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


# ── Per-service metric queries ────────────────────────────────────────────────

def get_current_metrics_all() -> dict[str, dict]:
    """
    Query Prometheus for current values of all key metrics across all services.
    Returns dict keyed by service_name.

    This is the main function called by context_builder to replace
    the Supabase latest_metrics view.
    """
    results = {svc: {} for svc in SERVICES}

    # Response time — p50, p95, p99
    for quantile, label in [(0.5, "p50"), (0.95, "p95"), (0.99, "p99")]:
        rows = _instant_query(
            f"histogram_quantile({quantile}, "
            f"rate(sre_request_duration_ms_bucket[2m]))"
        )
        for row in rows:
            port = row["metric"].get("instance", "")
            svc  = PORT_TO_SERVICE.get(port, row["metric"].get("service_name", ""))
            if svc in results:
                try:
                    results[svc][f"rt_{label}_ms"] = round(float(row["value"][1]), 2)
                except (ValueError, IndexError):
                    pass

    # Error rate
    for row in _instant_query("sre_error_rate_percent"):
        svc = _extract_service(row)
        if svc:
            try:
                results[svc]["error_rate_pct"] = round(float(row["value"][1]), 2)
            except (ValueError, IndexError):
                pass

    # Throughput
    for row in _instant_query("rate(sre_requests_total[1m]) * 60"):
        svc = _extract_service(row)
        if svc:
            try:
                results[svc]["throughput_rps"] = round(float(row["value"][1]), 2)
            except (ValueError, IndexError):
                pass

    # CPU
    for row in _instant_query("sre_cpu_percent"):
        svc = _extract_service(row)
        if svc:
            try:
                results[svc]["cpu_pct"] = round(float(row["value"][1]), 2)
            except (ValueError, IndexError):
                pass

    # Memory
    for row in _instant_query("sre_memory_percent"):
        svc = _extract_service(row)
        if svc:
            try:
                results[svc]["memory_pct"] = round(float(row["value"][1]), 2)
            except (ValueError, IndexError):
                pass

    # Uptime
    for row in _instant_query("sre_uptime_percent"):
        svc = _extract_service(row)
        if svc:
            try:
                results[svc]["uptime_pct"] = round(float(row["value"][1]), 2)
            except (ValueError, IndexError):
                pass

    # Degrading flag
    for row in _instant_query("sre_is_degrading"):
        svc = _extract_service(row)
        if svc:
            try:
                results[svc]["is_degrading"] = int(float(row["value"][1])) == 1
            except (ValueError, IndexError):
                pass

    return results


def get_recent_metrics_service(service_name: str, minutes: int = 30) -> list[dict]:
    """
    Get time-series data for a single service over the last N minutes.
    Returns a list of timestamped metric snapshots for the agent context table.
    """
    port = next(
        (p for p, s in PORT_TO_SERVICE.items() if s == service_name), None
    )
    if not port:
        return []

    # Get p95 latency time series
    rt_series = _range_query(
        f'histogram_quantile(0.95, rate(sre_request_duration_ms_bucket'
        f'{{instance="{port}"}}[2m]))',
        minutes=minutes,
    )
    er_series = _range_query(
        f'sre_error_rate_percent{{instance="{port}"}}',
        minutes=minutes,
    )
    tp_series = _range_query(
        f'rate(sre_requests_total{{instance="{port}"}}[1m]) * 60',
        minutes=minutes,
    )

    # Merge into timestamped rows
    rt_map = {}
    if rt_series:
        for ts, val in rt_series[0].get("values", []):
            rt_map[int(ts)] = round(float(val), 2) if val != "NaN" else None

    er_map = {}
    if er_series:
        for ts, val in er_series[0].get("values", []):
            er_map[int(ts)] = round(float(val), 2) if val != "NaN" else None

    tp_map = {}
    if tp_series:
        for ts, val in tp_series[0].get("values", []):
            tp_map[int(ts)] = round(float(val), 2) if val != "NaN" else None

    # Build merged list sorted by time
    all_ts = sorted(set(rt_map) | set(er_map) | set(tp_map))
    rows = []
    for ts in all_ts[-20:]:    # last 20 data points
        t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")
        rows.append({
            "time":             t,
            "rt_p95_ms":        rt_map.get(ts),
            "error_rate_pct":   er_map.get(ts),
            "throughput_rps":   tp_map.get(ts),
        })

    return rows


def _extract_service(row: dict) -> str | None:
    """Extract service name from a Prometheus result row."""
    metric = row.get("metric", {})
    # Try direct service_name label first
    if "service_name" in metric:
        return metric["service_name"]
    # Fall back to port-based lookup
    port = metric.get("instance", "")
    return PORT_TO_SERVICE.get(port)


# ── Formatted output for agent prompts ───────────────────────────────────────

def format_current_metrics_table() -> str:
    """
    Returns a formatted table of current metrics across all services.
    Used by context_builder to replace _format_all_latest().
    Includes p50/p95/p99 latency — richer than Supabase version.
    """
    metrics = get_current_metrics_all()

    if not any(metrics.values()):
        return "Prometheus unavailable — no live metrics."

    lines = [
        "Service               | p50(ms) | p95(ms) | p99(ms) | "
        "Error% | CPU%  | Mem%  | Throughput"
    ]
    lines.append("-" * 95)

    for svc in SERVICES:
        m   = metrics.get(svc, {})
        p50 = f"{m.get('rt_p50_ms', '?')}"
        p95 = f"{m.get('rt_p95_ms', '?')}"
        p99 = f"{m.get('rt_p99_ms', '?')}"
        er  = f"{m.get('error_rate_pct', '?')}"
        cpu = f"{m.get('cpu_pct', '?')}"
        mem = f"{m.get('memory_pct', '?')}"
        tp  = f"{m.get('throughput_rps', '?')}"
        deg = " ⚠ DEGRADING" if m.get("is_degrading") else ""
        lines.append(
            f"{svc[:22].ljust(22)} | {p50:>7} | {p95:>7} | {p99:>7} | "
            f"{er:>6} | {cpu:>5} | {mem:>5} | {tp:>10}{deg}"
        )

    return "\n".join(lines)


def format_recent_metrics_table(service_name: str, minutes: int = 30) -> str:
    """
    Returns last 30 min of p95 latency + error rate for one service.
    Used by context_builder for the per-service trend table in RCA/prediction.
    """
    rows = get_recent_metrics_service(service_name, minutes)
    if not rows:
        return f"No Prometheus data for {service_name} in last {minutes} min."

    lines = ["Time  | p95 RT(ms) | Error % | Throughput"]
    lines.append("-" * 45)
    for r in rows:
        rt  = f"{r['rt_p95_ms']:.0f}" if r["rt_p95_ms"] is not None else "?"
        er  = f"{r['error_rate_pct']:.1f}" if r["error_rate_pct"] is not None else "?"
        tp  = f"{r['throughput_rps']:.0f}" if r["throughput_rps"] is not None else "?"
        lines.append(f"{r['time']} | {rt:>10} | {er:>7} | {tp:>10}")

    return "\n".join(lines)
