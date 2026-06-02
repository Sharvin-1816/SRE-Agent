"""
collector/anomaly_detector.py

Z-score based anomaly detection + trend analysis.
Produces rich, human-readable LLM signals — not just "threshold exceeded".

Flow:
    raw metric row
        → compute z-scores against baseline
        → detect trend (rate of change, direction, duration)
        → find correlated services also degrading
        → format into plain-English LLM signal
        → write anomaly_event + llm_signal to DB
"""

import os
import math
from datetime import datetime, timezone, timedelta
from typing import Optional
from rich.console import Console

from db.database import (
    get_baseline, get_recent_metrics, get_latest_metric_per_service,
    insert_anomaly, insert_llm_signal, insert_alert
)

console = Console()

# ── Constants ─────────────────────────────────────────────────────────────────

ZSCORE_THRESHOLD  = float(os.getenv("ZSCORE_ANOMALY_THRESHOLD", "3.0"))
TREND_WINDOW_MINS = int(os.getenv("TREND_WINDOW_MINUTES", "30"))

# Which metrics to monitor and their human labels
MONITORED_METRICS = {
    "response_time_ms": {"label": "response time", "unit": "ms",  "direction": "high_is_bad"},
    "error_rate_pct":   {"label": "error rate",    "unit": "%",   "direction": "high_is_bad"},
    "throughput_rps":   {"label": "throughput",    "unit": "rps", "direction": "low_is_bad"},
    "cpu_pct":          {"label": "CPU usage",     "unit": "%",   "direction": "high_is_bad"},
    "memory_pct":       {"label": "memory usage",  "unit": "%",   "direction": "high_is_bad"},
}

# Baseline column mapping: metric name → (mean_col, std_col)
BASELINE_COLS = {
    "response_time_ms": ("rt_mean",  "rt_std"),
    "error_rate_pct":   ("er_mean",  "er_std"),
    "throughput_rps":   ("tp_mean",  "tp_std"),
    "cpu_pct":          ("cpu_mean", "cpu_std"),
    "memory_pct":       ("mem_mean", "mem_std"),
}

SEVERITY_LEVELS = [
    (8.0,  "critical"),
    (5.0,  "high"),
    (3.0,  "medium"),
    (0.0,  "low"),
]


def _get_time_window(hour: int) -> str:
    """Map hour of day to named time window matching baseline_profiles."""
    if   0  <= hour < 6:  return "weekday_overnight"
    elif 6  <= hour < 12: return "weekday_morning"
    elif 12 <= hour < 18: return "weekday_afternoon"
    else:                 return "weekday_evening"


def _severity_from_zscore(z: float) -> str:
    for threshold, label in SEVERITY_LEVELS:
        if abs(z) >= threshold:
            return label
    return "low"


def _compute_zscores(metric_row: dict, baseline: dict) -> list[dict]:
    """
    Compute z-score for each monitored metric.
    Returns list of anomalous metrics (|z| >= threshold).
    """
    anomalies = []
    for metric, info in MONITORED_METRICS.items():
        current = metric_row.get(metric)
        if current is None:
            continue

        mean_col, std_col = BASELINE_COLS[metric]
        mean = baseline.get(mean_col)
        std  = baseline.get(std_col)

        if mean is None or std is None or std < 0.001:
            continue  # not enough baseline data for this metric

        z = (current - mean) / std

        # For "low is bad" metrics (throughput), invert so high |z| = bad
        if info["direction"] == "low_is_bad":
            z = -z

        if abs(z) >= ZSCORE_THRESHOLD:
            anomalies.append({
                "metric":          metric,
                "label":           info["label"],
                "unit":            info["unit"],
                "current_value":   round(current, 2),
                "baseline_mean":   round(mean, 2),
                "baseline_std":    round(std, 4),
                "z_score":         round(z, 2),
                "severity":        _severity_from_zscore(z),
                "normal_range":    f"{round(mean - 2*std, 1)}–{round(mean + 2*std, 1)}{info['unit']}",
            })

    # Sort by severity of z-score
    anomalies.sort(key=lambda x: abs(x["z_score"]), reverse=True)
    return anomalies


def _compute_trend(service_name: str, metric: str) -> Optional[dict]:
    """
    Analyse trend for a metric over the last TREND_WINDOW_MINS.
    Returns trend dict or None if insufficient data.
    """
    recent = get_recent_metrics(service_name, minutes=TREND_WINDOW_MINS)
    if len(recent) < 4:
        return None

    values = [r.get(metric) for r in recent if r.get(metric) is not None]
    if len(values) < 4:
        return None

    n = len(values)
    # Simple linear regression to get slope
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    numerator   = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    slope = numerator / denominator if denominator > 0 else 0.0

    # Rate per minute (readings are every 60s)
    rate_per_min = round(slope, 3)

    # Direction
    if abs(rate_per_min) < 0.5:
        direction = "stable"
    elif rate_per_min > 0:
        direction = "increasing"
    else:
        direction = "decreasing"

    # Check if accelerating: compare slope of first half vs second half
    mid = n // 2
    first_half  = values[:mid]
    second_half = values[mid:]
    slope_first  = (first_half[-1]  - first_half[0])  / max(len(first_half)  - 1, 1)
    slope_second = (second_half[-1] - second_half[0]) / max(len(second_half) - 1, 1)
    is_accelerating = (slope_second > slope_first * 1.3) and direction == "increasing"

    # How long has this trend been sustained?
    # Find how many consecutive readings have been above/below mean
    sustained_count = 0
    last_val = values[-1]
    mid_val  = values[n // 2]
    for v in reversed(values):
        if (last_val > mid_val and v > y_mean) or (last_val < mid_val and v < y_mean):
            sustained_count += 1
        else:
            break

    return {
        "direction":       direction,
        "rate_per_min":    rate_per_min,
        "duration_mins":   sustained_count,
        "is_accelerating": is_accelerating,
        "first_value":     round(values[0],  2),
        "last_value":      round(values[-1], 2),
        "sample_count":    n,
    }


def _find_correlated_services(primary_service: str, detected_at: datetime) -> list[dict]:
    """
    Check if other services are also currently degrading.
    Uses latest metrics for each service.
    """
    all_latest = get_latest_metric_per_service()
    correlated = []

    for row in all_latest:
        svc = row["service_name"]
        if svc == primary_service:
            continue

        hour        = detected_at.hour
        time_window = _get_time_window(hour)
        baseline    = get_baseline(svc, time_window)
        if not baseline:
            continue

        # Quick check: just response time z-score for correlation
        rt     = row.get("response_time_ms")
        mean   = baseline.get("rt_mean")
        std    = baseline.get("rt_std")
        if rt and mean and std and std > 0.001:
            z = (rt - mean) / std
            if abs(z) >= ZSCORE_THRESHOLD * 0.8:  # slightly lower bar for correlation
                correlated.append({
                    "service":         svc,
                    "also_degrading":  True,
                    "z_score":         round(z, 2),
                    "response_time_ms": round(rt, 2),
                })
            else:
                correlated.append({
                    "service":         svc,
                    "also_degrading":  False,
                    "z_score":         round(z, 2),
                })

    return correlated


def _format_llm_signal(
    service_name:       str,
    anomalies:          list[dict],
    trends:             dict,
    correlated:         list[dict],
    baseline:           dict,
    time_window:        str,
    detected_at:        datetime,
) -> dict:
    """
    Convert raw z-scores and trends into a rich plain-English signal
    the LLM can directly reason about — no number crunching needed by the LLM.
    """
    max_z    = max(abs(a["z_score"]) for a in anomalies)
    severity = _severity_from_zscore(max_z)
    primary  = anomalies[0]  # worst metric

    # ── Build human summary ──────────────────────────────────────────────────
    parts = []

    # Opening: what is happening
    parts.append(
        f"{service_name.replace('_', ' ').title()} {primary['label']} is "
        f"{primary['current_value']}{primary['unit']} — "
        f"{abs(primary['z_score']):.1f} standard deviations "
        f"{'above' if primary['z_score'] > 0 else 'below'} its normal "
        f"{time_window.replace('weekday_', '').replace('_', ' ')} baseline "
        f"(normal range: {primary['normal_range']})."
    )

    # Trend narrative
    trend = trends.get(primary["metric"])
    if trend and trend["direction"] != "stable":
        accel_str = " The rate of increase is accelerating." if trend["is_accelerating"] else ""
        parts.append(
            f"This metric has been {trend['direction']} for "
            f"{trend['duration_mins']} consecutive readings "
            f"at a rate of {abs(trend['rate_per_min']):.1f}{primary['unit']}/min.{accel_str}"
        )
    elif trend and trend["direction"] == "stable":
        parts.append("This appears to be a sudden spike rather than a gradual degradation.")

    # Secondary anomalies
    if len(anomalies) > 1:
        others = ", ".join(
            f"{a['label']} ({a['z_score']:.1f}σ)"
            for a in anomalies[1:]
        )
        parts.append(f"Additional metrics also anomalous: {others}.")

    # Correlation narrative
    also_bad = [c for c in correlated if c["also_degrading"]]
    if also_bad:
        svc_list = ", ".join(c["service"].replace("_", " ") for c in also_bad)
        parts.append(
            f"{len(also_bad)} other service(s) are also currently degrading: {svc_list}. "
            f"This pattern suggests a shared dependency failure rather than an isolated issue."
        )
    else:
        parts.append(
            "No other services are currently degrading — this appears to be an isolated failure."
        )

    human_summary = " ".join(parts)

    # ── Metrics snapshot (structured, for LLM table context) ────────────────
    metrics_snapshot = {}
    for a in anomalies:
        metrics_snapshot[a["metric"]] = {
            "now":          a["current_value"],
            "normal_range": a["normal_range"],
            "z_score":      a["z_score"],
            "severity":     a["severity"],
        }

    # ── Trend summary (plain text) ───────────────────────────────────────────
    trend_parts = []
    for metric, t in trends.items():
        if t and t["direction"] != "stable":
            label = MONITORED_METRICS.get(metric, {}).get("label", metric)
            trend_parts.append(
                f"{label}: {t['direction']} for {t['duration_mins']} min "
                f"({t['first_value']} → {t['last_value']})"
            )
    trend_summary = "; ".join(trend_parts) if trend_parts else "No sustained trends detected."

    # ── Context window description ───────────────────────────────────────────
    window_descriptions = {
        "weekday_overnight":  "Overnight (00:00–06:00). Historically very low traffic. Any anomaly here is significant.",
        "weekday_morning":    "Morning (06:00–12:00). Ramping traffic. Baseline reflects increasing load.",
        "weekday_afternoon":  "Afternoon (12:00–18:00). Peak business hours. Highest baseline traffic period.",
        "weekday_evening":    "Evening (18:00–24:00). Tapering load after peak.",
    }
    context_window = window_descriptions.get(time_window, time_window)

    # ── Hypothesis hints (reasoning shortcuts for the LLM) ───────────────────
    hints = []
    if also_bad:
        hints.append(
            f"{len(also_bad)} services degrading simultaneously — likely shared "
            "dependency (database, message queue, or network)"
        )
    rt_anom  = next((a for a in anomalies if a["metric"] == "response_time_ms"), None)
    tp_anom  = next((a for a in anomalies if a["metric"] == "throughput_rps"),   None)
    err_anom = next((a for a in anomalies if a["metric"] == "error_rate_pct"),   None)

    if rt_anom and tp_anom and rt_anom["z_score"] > 0 and tp_anom["z_score"] < 0:
        hints.append(
            "Response time rising while throughput falls — classic symptom of "
            "connection pool exhaustion or upstream queue backup"
        )
    if err_anom and not rt_anom:
        hints.append(
            "Error rate spiking without significant latency increase — "
            "suggests application-level errors (bad config, auth failures, logic bugs) "
            "rather than infrastructure overload"
        )
    if trend and trend.get("is_accelerating"):
        hints.append(
            "Degradation rate is accelerating — service likely to reach failure "
            "point soon without intervention"
        )
    if not hints:
        hints.append("Isolated metric anomaly — check recent deployments or config changes")

    return {
        "service_name":         service_name,
        "severity":             severity,
        "human_summary":        human_summary,
        "metrics_snapshot":     metrics_snapshot,
        "trend_summary":        trend_summary,
        "context_window":       context_window,
        "hypothesis_hints":     hints,
        "correlated_services":  correlated,
    }


# ── Public interface ──────────────────────────────────────────────────────────

def check_for_anomalies(metric_row: dict) -> Optional[dict]:
    """
    Main entry point called by the collector after every metric write.

    Returns the llm_signal dict if an anomaly was detected, None otherwise.
    """
    service_name = metric_row["service_name"]
    detected_at  = datetime.now(timezone.utc)
    time_window  = _get_time_window(detected_at.hour)

    # 1. Get baseline for this service + time window
    baseline = get_baseline(service_name, time_window)
    if baseline is None:
        # Not enough historical data yet — fall back to loose absolute thresholds
        _check_absolute_fallback(metric_row, service_name, detected_at)
        return None

    # 2. Compute z-scores
    anomalies = _compute_zscores(metric_row, baseline)
    if not anomalies:
        return None  # everything normal

    # 3. Compute trends for each anomalous metric
    trends = {}
    for a in anomalies:
        trends[a["metric"]] = _compute_trend(service_name, a["metric"])

    # 4. Find correlated services
    correlated = _find_correlated_services(service_name, detected_at)

    # 5. Format rich LLM signal
    signal_data = _format_llm_signal(
        service_name, anomalies, trends, correlated,
        baseline, time_window, detected_at
    )

    max_z    = max(abs(a["z_score"]) for a in anomalies)
    severity = signal_data["severity"]

    # 6. Write anomaly event to DB
    event = insert_anomaly({
        "service_name":        service_name,
        "detected_at":         detected_at.isoformat(),
        "max_z_score":         round(max_z, 2),
        "severity":            severity,
        "anomalies":           anomalies,
        "trend":               trends,
        "correlated_services": correlated,
        "processed_by_agent":  False,
    })

    # 7. Write LLM signal
    signal = insert_llm_signal({
        **signal_data,
        "anomaly_event_id": event["id"],
        "generated_at":     detected_at.isoformat(),
    })

    # 8. Write individual alerts (one per anomalous metric)
    for a in anomalies:
        insert_alert({
            "service_name": service_name,
            "anomaly_id":   event["id"],
            "triggered_at": detected_at.isoformat(),
            "metric":       a["metric"],
            "severity":     a["severity"],
            "message": (
                f"{a['label']} is {a['current_value']}{a['unit']} "
                f"({a['z_score']:.1f}σ above baseline, normal: {a['normal_range']})"
            ),
        })

    console.print(
        f"[bold red]  ⚠ ANOMALY[/bold red] {service_name} | "
        f"severity={severity} | max_z={max_z:.1f} | "
        f"{len(anomalies)} metric(s) anomalous"
    )

    return signal


def _check_absolute_fallback(metric_row: dict, service_name: str, detected_at: datetime):
    """
    Used when baseline isn't built yet (< 50 samples).
    Very loose absolute limits so we catch obvious crashes.
    """
    FALLBACK = {
        "response_time_ms": 5000,
        "error_rate_pct":   20.0,
        "cpu_pct":          95.0,
        "memory_pct":       95.0,
    }
    for metric, limit in FALLBACK.items():
        val = metric_row.get(metric)
        if val and val > limit:
            insert_alert({
                "service_name": service_name,
                "anomaly_id":   None,
                "triggered_at": detected_at.isoformat(),
                "metric":       metric,
                "severity":     "high",
                "message": (
                    f"[FALLBACK] {metric} = {val} exceeds absolute limit {limit}. "
                    "Baseline not yet established."
                ),
            })
