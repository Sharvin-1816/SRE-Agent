"""
db/seed.py
Populate Supabase with 7 days of realistic mock data for all 6 services.
Run once after applying schema.sql and rpc_functions.sql.

    python db/seed.py

Generates:
  - 7 days × 1440 min/day / 1 reading per min = ~10,080 raw metric rows
    (we use 60s intervals = 10,080 rows per service = 60,480 total)
  - Baseline profiles computed from that data
  - A few sample anomaly events + LLM signals
  - Sample context store entries
  - Sample incidents
"""

import random
import math
from datetime import datetime, timezone, timedelta
from rich.console import Console
from rich.progress import track
from db.database import (
    insert_metric, upsert_baseline, insert_anomaly,
    insert_llm_signal, add_user_context, insert_alert,
    insert_incident, mark_alerts_grouped, db
)

console = Console()
random.seed(42)

# ── Service definitions ───────────────────────────────────────────────────────
# Each service has realistic baseline characteristics + failure personalities

SERVICES = {
    "payment_service": {
        "base_rt": 410,      # baseline response time ms
        "base_er": 0.8,      # baseline error rate %
        "base_tp": 155,      # baseline throughput rps
        "base_cpu": 52,
        "base_mem": 61,
        "rt_noise": 30,      # std dev of normal noise
        "er_noise": 0.4,
        "tp_noise": 18,
        "port": 3001,
    },
    "cart_service": {
        "base_rt": 280,
        "base_er": 0.5,
        "base_tp": 210,
        "base_cpu": 38,
        "base_mem": 44,
        "rt_noise": 22,
        "er_noise": 0.3,
        "tp_noise": 25,
        "port": 3002,
    },
    "notification_service": {
        "base_rt": 190,
        "base_er": 1.1,
        "base_tp": 320,
        "base_cpu": 29,
        "base_mem": 35,
        "rt_noise": 18,
        "er_noise": 0.6,
        "tp_noise": 40,
        "port": 3003,
    },
    "auth_service": {
        "base_rt": 95,
        "base_er": 0.2,
        "base_tp": 480,
        "base_cpu": 22,
        "base_mem": 28,
        "rt_noise": 10,
        "er_noise": 0.15,
        "tp_noise": 35,
        "port": 3004,
    },
    "inventory_service": {
        "base_rt": 620,
        "base_er": 1.5,
        "base_tp": 88,
        "base_cpu": 65,
        "base_mem": 72,
        "rt_noise": 55,
        "er_noise": 0.8,
        "tp_noise": 12,
        "port": 3005,
    },
    "gateway_service": {
        "base_rt": 45,
        "base_er": 0.1,
        "base_tp": 890,
        "base_cpu": 18,
        "base_mem": 22,
        "rt_noise": 8,
        "er_noise": 0.08,
        "tp_noise": 60,
        "port": 3006,
    },
}

# Time-of-day multipliers — load varies through the day
# Index = hour of day (0-23)
LOAD_CURVE = [
    0.3, 0.2, 0.2, 0.2, 0.2, 0.3,   # 00-05 low overnight
    0.5, 0.7, 0.9, 1.0, 1.0, 1.0,   # 06-11 morning ramp
    1.1, 1.1, 1.0, 1.0, 1.1, 1.2,   # 12-17 afternoon peak
    1.3, 1.2, 1.0, 0.8, 0.6, 0.4,   # 18-23 evening taper
]

TIME_WINDOWS = [
    ("overnight",       0,  6),
    ("morning",         6, 12),
    ("afternoon",      12, 18),
    ("evening",        18, 24),
]


def _noisy(base: float, noise: float, multiplier: float = 1.0) -> float:
    """Gaussian noise around a baseline, scaled by load multiplier."""
    value = base * multiplier + random.gauss(0, noise)
    return round(max(0.0, value), 2)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def generate_metric_row(
    service_name: str,
    svc: dict,
    ts: datetime,
    inject_anomaly: bool = False,
    anomaly_intensity: float = 1.0,
) -> dict:
    """
    Generate one realistic metric reading for a service at a given timestamp.
    inject_anomaly=True spikes the metrics to simulate an incident.
    """
    hour = ts.hour
    load_mult = LOAD_CURVE[hour]

    if inject_anomaly:
        # Spike: response time 4-15x normal, error rate jumps, throughput drops
        rt   = _noisy(svc["base_rt"] * (4 + anomaly_intensity * 11), svc["rt_noise"] * 3)
        er   = _clamp(_noisy(svc["base_er"] * (5 + anomaly_intensity * 8), svc["er_noise"] * 4), 0, 100)
        tp   = _noisy(svc["base_tp"] * max(0.1, 1 - anomaly_intensity * 0.7), svc["tp_noise"])
        cpu  = _clamp(_noisy(svc["base_cpu"] * (1.5 + anomaly_intensity), 5), 0, 100)
        mem  = _clamp(_noisy(svc["base_mem"] * (1.3 + anomaly_intensity * 0.5), 5), 0, 100)
        reachable = anomaly_intensity < 0.9
        status = 200 if reachable else 503
    else:
        rt   = _noisy(svc["base_rt"],  svc["rt_noise"],  load_mult)
        er   = _clamp(_noisy(svc["base_er"],  svc["er_noise"],  load_mult * 0.5), 0, 100)
        tp   = _noisy(svc["base_tp"],  svc["tp_noise"],  load_mult)
        cpu  = _clamp(_noisy(svc["base_cpu"], svc["rt_noise"] * 0.1, load_mult), 0, 100)
        mem  = _clamp(_noisy(svc["base_mem"], 3), 0, 100)
        reachable = True
        status = 200

    return {
        "service_name":     service_name,
        "timestamp":        ts.isoformat(),
        "response_time_ms": rt,
        "error_rate_pct":   er,
        "throughput_rps":   tp,
        "uptime_pct":       100.0 if reachable else round(random.uniform(60, 90), 2),
        "upload_time_ms":   round(rt * random.uniform(0.3, 0.6), 2),
        "cpu_pct":          cpu,
        "memory_pct":       mem,
        "status_code":      status,
        "is_reachable":     reachable,
        "error_message":    None if reachable else "Connection timeout",
    }


# ── Anomaly scenarios injected into the historical data ──────────────────────

ANOMALY_SCENARIOS = [
    # (service, days_ago, start_hour, duration_minutes, intensity)
    ("payment_service",      2, 14, 90,  0.7),   # gradual degradation yesterday afternoon
    ("inventory_service",    4, 9, 45,  0.9),   # brief spike 4 days ago
    ("cart_service",         1, 20, 30,  0.6),   # evening incident yesterday
    ("gateway_service",      5, 2, 15,  1.0),   # full outage overnight 5 days ago
]


def _is_anomaly_window(ts: datetime, scenarios: list) -> tuple[bool, float]:
    """Check if a given timestamp falls inside any injected anomaly window."""
    now = datetime.now(timezone.utc)
    for service, days_ago, start_hour, duration, intensity in scenarios:
        incident_start = (now - timedelta(days=days_ago)).replace(
            hour=start_hour, minute=0, second=0, microsecond=0,
            tzinfo=timezone.utc
        )
        incident_end = incident_start + timedelta(minutes=duration)
        if incident_start <= ts <= incident_end:
            # Ramp intensity: starts low, peaks at middle, tapers
            elapsed = (ts - incident_start).total_seconds() / 60
            ramp = math.sin(math.pi * elapsed / duration)
            return True, intensity * ramp
    return False, 0.0


# ── Main seed functions ───────────────────────────────────────────────────────

def seed_metrics():
    """Insert 7 days of metric rows for all services."""
    console.print("[bold cyan]Seeding metrics_raw...[/bold cyan]")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    interval = timedelta(minutes=1)

    # Build timestamps list
    timestamps = []
    t = start
    while t <= now:
        timestamps.append(t)
        t += interval

    console.print(f"  {len(timestamps)} timestamps × {len(SERVICES)} services = "
                  f"{len(timestamps) * len(SERVICES):,} rows")

    for service_name, svc in SERVICES.items():
        console.print(f"  Inserting {service_name}...")
        batch = []
        for ts in timestamps:
            is_anom, intensity = _is_anomaly_window(ts, [
                s for s in ANOMALY_SCENARIOS if s[0] == service_name
            ])
            row = generate_metric_row(service_name, svc, ts, is_anom, intensity)
            batch.append(row)
            if len(batch) >= 500:          # Supabase batch insert limit
                db().table("metrics_raw").insert(batch).execute()
                batch = []
        if batch:
            db().table("metrics_raw").insert(batch).execute()

    console.print("[green]  metrics_raw done[/green]")


def seed_baselines():
    """Compute and insert baseline profiles from the seeded metrics."""
    console.print("[bold cyan]Computing baseline_profiles...[/bold cyan]")

    import numpy as np

    now = datetime.now(timezone.utc)

    for service_name, svc in SERVICES.items():
        for window_name, h_start, h_end in TIME_WINDOWS:
            rows = db().rpc("get_historical_window", {
                "p_service":    service_name,
                "p_hour_start": h_start,
                "p_hour_end":   h_end,
                "p_days_back":  7,
            }).execute().data

            if len(rows) < 10:
                continue

            rt  = [r["response_time_ms"] for r in rows if r["response_time_ms"]]
            er  = [r["error_rate_pct"]   for r in rows if r["error_rate_pct"] is not None]
            tp  = [r["throughput_rps"]   for r in rows if r["throughput_rps"]]
            cpu = [r["cpu_pct"]          for r in rows if r["cpu_pct"]]
            mem = [r["memory_pct"]       for r in rows if r["memory_pct"]]

            profile = {
                "service_name": service_name,
                "time_window":  f"weekday_{window_name}",
                "hour_start":   h_start,
                "hour_end":     h_end,
                "sample_count": len(rows),
                "rt_mean":  round(float(np.mean(rt)),  2),
                "rt_std":   round(float(np.std(rt)),   2),
                "er_mean":  round(float(np.mean(er)),  4),
                "er_std":   round(float(np.std(er)),   4),
                "tp_mean":  round(float(np.mean(tp)),  2),
                "tp_std":   round(float(np.std(tp)),   2),
                "cpu_mean": round(float(np.mean(cpu)), 2),
                "cpu_std":  round(float(np.std(cpu)),  2),
                "mem_mean": round(float(np.mean(mem)), 2),
                "mem_std":  round(float(np.std(mem)),  2),
            }
            upsert_baseline(profile)
            console.print(f"  {service_name} / {window_name} ({len(rows)} samples)")

    console.print("[green]  baseline_profiles done[/green]")


def seed_anomaly_events():
    """Insert sample anomaly events matching the injected scenarios."""
    console.print("[bold cyan]Seeding anomaly_events + llm_signals...[/bold cyan]")

    now = datetime.now(timezone.utc)
    scenarios = [
        {
            "service_name": "payment_service",
            "days_ago": 2,
            "max_z_score": 15.3,
            "severity": "critical",
            "anomalies": [
                {"metric": "response_time_ms", "current_value": 847,
                 "baseline_mean": 410.0, "baseline_std": 28.5, "z_score": 15.3},
                {"metric": "error_rate_pct",   "current_value": 3.2,
                 "baseline_mean": 0.8,   "baseline_std": 0.4,  "z_score": 6.0},
            ],
            "trend": {
                "response_time_ms": {
                    "direction": "increasing",
                    "rate_per_min": 38.4,
                    "duration_mins": 42,
                    "is_accelerating": True,
                }
            },
            "correlated_services": [
                {"service": "cart_service",    "also_degrading": True,  "z_score": 4.1},
                {"service": "gateway_service", "also_degrading": True,  "z_score": 3.7},
                {"service": "auth_service",    "also_degrading": False, "z_score": 0.8},
            ],
            "human_summary": (
                "Payment service response time is 15.3 standard deviations above its normal "
                "afternoon baseline and has been steadily increasing for 42 minutes, accelerating "
                "at 38ms per minute. Error rate is also 6 standard deviations above baseline. "
                "This is not a random spike — the trend is sustained and worsening. "
                "Cart and Gateway services are also degrading simultaneously, suggesting a "
                "shared upstream dependency issue rather than an isolated failure."
            ),
        },
        {
            "service_name": "gateway_service",
            "days_ago": 5,
            "max_z_score": 22.1,
            "severity": "critical",
            "anomalies": [
                {"metric": "response_time_ms", "current_value": 9800,
                 "baseline_mean": 45.0, "baseline_std": 8.0, "z_score": 22.1},
                {"metric": "uptime_pct", "current_value": 0.0,
                 "baseline_mean": 100.0, "baseline_std": 0.1, "z_score": -999},
            ],
            "trend": {
                "response_time_ms": {
                    "direction": "sudden_spike",
                    "rate_per_min": 980.0,
                    "duration_mins": 3,
                    "is_accelerating": False,
                }
            },
            "correlated_services": [],
            "human_summary": (
                "Gateway service is completely unreachable. Response time spiked to 9800ms "
                "(22 standard deviations above normal) and uptime dropped to 0%. "
                "This is a sudden full outage, not a gradual degradation. "
                "Occurred at 2 AM — low baseline traffic period. No other services affected, "
                "suggesting an isolated gateway process crash rather than infrastructure failure."
            ),
        },
    ]

    for s in scenarios:
        detected_at = (now - timedelta(days=s["days_ago"])).replace(
            hour=14, minute=32, second=0, microsecond=0
        ).isoformat()

        event = insert_anomaly({
            "service_name":         s["service_name"],
            "detected_at":          detected_at,
            "max_z_score":          s["max_z_score"],
            "severity":             s["severity"],
            "anomalies":            s["anomalies"],
            "trend":                s["trend"],
            "correlated_services":  s["correlated_services"],
            "processed_by_agent":   True,
        })

        # Insert corresponding LLM signal
        metrics_snapshot = {}
        for a in s["anomalies"]:
            m = a["metric"]
            metrics_snapshot[m] = {
                "now": a["current_value"],
                "normal_range": f'{round(a["baseline_mean"] - 2*a["baseline_std"], 1)}–'
                                f'{round(a["baseline_mean"] + 2*a["baseline_std"], 1)}',
                "z_score": a["z_score"],
            }

        insert_llm_signal({
            "anomaly_event_id":     event["id"],
            "service_name":         s["service_name"],
            "generated_at":         detected_at,
            "severity":             s["severity"],
            "human_summary":        s["human_summary"],
            "metrics_snapshot":     metrics_snapshot,
            "trend_summary":        next(iter(s["trend"].values()), {}).get("direction", ""),
            "context_window":       "Afternoon (12:00–18:00). Historically moderate load.",
            "hypothesis_hints": [
                "Multiple services degrading simultaneously suggests shared dependency",
                "Throughput dropping while response times rise — connection queue backup",
                "No deployment events recorded in the last 2 hours",
            ],
            "correlated_services": s["correlated_services"],
        })

        console.print(f"  {s['service_name']} anomaly event + signal seeded")

    console.print("[green]  anomaly_events + llm_signals done[/green]")


def seed_context_store():
    """Insert sample free-text context entries exactly as a user would type them."""
    console.print("[bold cyan]Seeding context_store...[/bold cyan]")

    from db.database import add_user_context

    entries = [
        # User just types plain English — no keys, no structure
        ("user_provided", "There will be a power outage on 3rd March from 2 AM to 6 AM. All services may be affected."),
        ("user_provided", "New movie releasing on 9th April. Expecting massive traffic spike on the streaming and notification services."),
        ("user_provided", "Flash sale runs every Friday evening between 6 PM and 9 PM. Payment and cart services see 3-4x normal load."),
        ("user_provided", "Deployment of payment service v2.3 happened today at 12:15 PM. Changes include a new checkout flow and updated DB queries."),
        ("system",        "Baseline profiles computed from 7 days of historical data across all 6 services."),
    ]

    for source, text in entries:
        add_user_context(text, source=source)
        console.print(f"  + {text[:70]}...")

    console.print("[green]  context_store done[/green]")


def seed_incidents():
    """Insert sample grouped incidents."""
    console.print("[bold cyan]Seeding alerts + incidents...[/bold cyan]")

    now = datetime.now(timezone.utc)

    # Alert batch for the payment incident
    alert_ids = []
    alert_messages = [
        ("payment_service",      "response_time_ms", "critical", "Response time 847ms — 15.3σ above baseline"),
        ("payment_service",      "error_rate_pct",   "high",     "Error rate 3.2% — 6.0σ above baseline"),
        ("cart_service",         "response_time_ms", "high",     "Response time elevated — 4.1σ above baseline"),
        ("gateway_service",      "response_time_ms", "high",     "Gateway latency spike — 3.7σ above baseline"),
        ("cart_service",         "error_rate_pct",   "medium",   "Cart error rate rising"),
        ("notification_service", "response_time_ms", "low",      "Minor latency increase on notification service"),
        ("payment_service",      "throughput_rps",   "medium",   "Throughput dropped 37% below baseline"),
    ]

    triggered_at = (now - timedelta(days=2, hours=0, minutes=32)).isoformat()

    for svc, metric, sev, msg in alert_messages:
        alert = insert_alert({
            "service_name": svc,
            "anomaly_id":   None,
            "triggered_at": triggered_at,
            "metric":       metric,
            "severity":     sev,
            "message":      msg,
        })
        alert_ids.append(alert["id"])

    # Group into one incident
    incident = insert_incident({
        "title":              "Payment + Gateway shared dependency degradation",
        "created_at":         triggered_at,
        "affected_services":  ["payment_service", "cart_service", "gateway_service"],
        "raw_alert_count":    7,
        "suppressed_count":   6,
        "status":             "resolved",
    })

    mark_alerts_grouped(alert_ids, incident["id"])
    console.print(f"  Incident '{incident['title']}' — 7 alerts → 1 incident")
    console.print("[green]  alerts + incidents done[/green]")


def main():
    console.rule("[bold]SRE Agent — Database Seed[/bold]")
    console.print("This will populate your Supabase database with 7 days of realistic data.\n")

    seed_metrics()
    seed_baselines()
    seed_anomaly_events()
    seed_context_store()
    seed_incidents()

    console.rule("[bold green]Seed complete![/bold green]")
    console.print("\nYour database now contains:")
    console.print("  • 7 days of metric readings for 6 services")
    console.print("  • Baseline profiles for all services × time windows")
    console.print("  • 2 sample anomaly events with LLM signals")
    console.print("  • Pre-fed context (Black Friday, deployment, maintenance)")
    console.print("  • 1 sample grouped incident (7 alerts → 1)")
    console.print("\nRun the collector next: [bold]python collector/collector.py[/bold]")


if __name__ == "__main__":
    main()