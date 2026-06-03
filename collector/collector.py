"""
collector/collector.py

Polls all 6 mock services every 60 seconds.
On each reading:
  1. Stores raw metrics to Supabase
  2. Runs anomaly detection (Z-score + trend)
  3. Updates baseline profiles incrementally
  4. Triggers agent if anomaly detected

Run: python -m collector.collector
"""

import os
import sys
import time
import httpx
import signal
import numpy as np
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from rich.console import Console
from rich.table import Table
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import (
    insert_metric, upsert_baseline, get_historical_window,
    get_latest_metric_per_service
)
from collector.anomaly_detector import check_for_anomalies

console = Console()

# ── Service registry ──────────────────────────────────────────────────────────

SERVICES = {
    "payment_service":      "http://localhost:3001",
    "cart_service":         "http://localhost:3002",
    "notification_service": "http://localhost:3003",
    "auth_service":         "http://localhost:3004",
    "inventory_service":    "http://localhost:3005",
    "gateway_service":      "http://localhost:3006",
}

POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_SECONDS",   "60"))
BASELINE_EVERY  = 10   # recompute baseline every N polls


# ── Poll a single service ─────────────────────────────────────────────────────

def poll_service(service_name: str, base_url: str) -> dict:
    """
    Ping the service's /metrics endpoint and return a normalised metric row.
    If the service is unreachable, returns a row marking it down.
    """
    start = time.monotonic()
    try:
        resp = httpx.get(f"{base_url}/metrics/json", timeout=5.0)
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)

        if resp.status_code == 200:
            data = resp.json()
            return {
                "service_name":     service_name,
                "response_time_ms": elapsed_ms,
                "error_rate_pct":   data.get("error_rate_pct",  0.0),
                "throughput_rps":   data.get("throughput_rps",  0.0),
                "uptime_pct":       data.get("uptime_pct",     100.0),
                "upload_time_ms":   data.get("upload_time_ms", elapsed_ms * 0.4),
                "cpu_pct":          data.get("cpu_pct",         0.0),
                "memory_pct":       data.get("memory_pct",      0.0),
                "status_code":      resp.status_code,
                "is_reachable":     True,
                "error_message":    None,
            }
        else:
            return _unreachable_row(service_name, resp.status_code,
                                    f"HTTP {resp.status_code}", elapsed_ms)

    except httpx.TimeoutException:
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        return _unreachable_row(service_name, 0, "Timeout after 5s", elapsed_ms)

    except httpx.ConnectError:
        return _unreachable_row(service_name, 0, "Connection refused", 0)

    except Exception as e:
        return _unreachable_row(service_name, 0, str(e), 0)


def _unreachable_row(service_name: str, status: int, error: str, rt: float) -> dict:
    return {
        "service_name":     service_name,
        "response_time_ms": rt if rt > 0 else 9999.0,
        "error_rate_pct":   100.0,
        "throughput_rps":   0.0,
        "uptime_pct":       0.0,
        "upload_time_ms":   0.0,
        "cpu_pct":          0.0,
        "memory_pct":       0.0,
        "status_code":      status,
        "is_reachable":     False,
        "error_message":    error,
    }


# ── Baseline updater ──────────────────────────────────────────────────────────

def _get_time_window(hour: int) -> tuple[str, int, int]:
    """Returns (window_name, hour_start, hour_end)."""
    if   0  <= hour < 6:  return "weekday_overnight",  0,  6
    elif 6  <= hour < 12: return "weekday_morning",    6, 12
    elif 12 <= hour < 18: return "weekday_afternoon", 12, 18
    else:                 return "weekday_evening",   18, 24


def update_baseline(service_name: str):
    """
    Recompute and upsert the baseline profile for the current time window.
    Called every BASELINE_EVERY polls.
    """
    now         = datetime.now(timezone.utc)
    window_name, h_start, h_end = _get_time_window(now.hour)

    try:
        rows = get_historical_window(service_name, h_start, h_end, days_back=7)
    except Exception as e:
        console.print(f"[yellow]  Baseline update skipped for {service_name}: {e}[/yellow]")
        return

    if len(rows) < 10:
        return  # not enough data yet

    def _stats(values):
        arr = [v for v in values if v is not None]
        if not arr:
            return None, None
        return round(float(np.mean(arr)), 4), round(float(np.std(arr)), 4)

    rt_m,  rt_s  = _stats([r["response_time_ms"] for r in rows])
    er_m,  er_s  = _stats([r["error_rate_pct"]   for r in rows])
    tp_m,  tp_s  = _stats([r["throughput_rps"]   for r in rows])
    cpu_m, cpu_s = _stats([r["cpu_pct"]           for r in rows])
    mem_m, mem_s = _stats([r["memory_pct"]        for r in rows])

    upsert_baseline({
        "service_name": service_name,
        "time_window":  window_name,
        "hour_start":   h_start,
        "hour_end":     h_end,
        "sample_count": len(rows),
        "rt_mean": rt_m, "rt_std": rt_s,
        "er_mean": er_m, "er_std": er_s,
        "tp_mean": tp_m, "tp_std": tp_s,
        "cpu_mean": cpu_m, "cpu_std": cpu_s,
        "mem_mean": mem_m, "mem_std": mem_s,
    })


# ── Display ───────────────────────────────────────────────────────────────────

def _print_poll_summary(results: list[tuple[str, dict]]):
    """Print a clean table of current service health to the terminal."""
    table = Table(title=f"[bold]Poll — {datetime.now().strftime('%H:%M:%S')}[/bold]",
                  show_lines=False)
    table.add_column("Service",      style="cyan",  width=24)
    table.add_column("Status",       style="white", width=8)
    table.add_column("RT (ms)",      justify="right")
    table.add_column("Error %",      justify="right")
    table.add_column("Throughput",   justify="right")
    table.add_column("CPU %",        justify="right")
    table.add_column("Memory %",     justify="right")

    for service_name, row in results:
        ok   = row["is_reachable"]
        rt   = row["response_time_ms"]
        er   = row["error_rate_pct"]
        tp   = row["throughput_rps"]
        cpu  = row["cpu_pct"]
        mem  = row["memory_pct"]

        status_str = "[green]UP[/green]" if ok else "[red]DOWN[/red]"
        rt_str     = f"[red]{rt:.0f}[/red]"   if rt  > 1000 else f"{rt:.0f}"
        er_str     = f"[red]{er:.1f}[/red]"   if er  > 5    else f"{er:.1f}"
        tp_str     = f"[yellow]{tp:.0f}[/yellow]" if tp < 50 else f"{tp:.0f}"
        cpu_str    = f"[red]{cpu:.1f}[/red]"  if cpu > 85   else f"{cpu:.1f}"
        mem_str    = f"[red]{mem:.1f}[/red]"  if mem > 85   else f"{mem:.1f}"

        table.add_row(service_name, status_str, rt_str, er_str, tp_str, cpu_str, mem_str)

    console.print(table)


# ── Core poll job ─────────────────────────────────────────────────────────────

_poll_count = 0

def poll_all_services():
    """
    Called by APScheduler every POLL_INTERVAL seconds.
    Polls all services, writes metrics, runs anomaly detection.
    """
    global _poll_count
    _poll_count += 1

    results   = []
    anomalies = []

    for service_name, base_url in SERVICES.items():
        row = poll_service(service_name, base_url)

        # Write to DB
        try:
            insert_metric(row)
        except Exception as e:
            console.print(f"[red]  DB write failed for {service_name}: {e}[/red]")
            continue

        # Anomaly detection on every reading
        try:
            signal = check_for_anomalies(row)
            if signal:
                anomalies.append((service_name, signal))
        except Exception as e:
            console.print(f"[yellow]  Anomaly check error for {service_name}: {e}[/yellow]")

        results.append((service_name, row))

        # Update baseline every N polls
        if _poll_count % BASELINE_EVERY == 0:
            try:
                update_baseline(service_name)
            except Exception as e:
                console.print(f"[yellow]  Baseline update failed for {service_name}: {e}[/yellow]")

    _print_poll_summary(results)

    # If anomalies found, only trigger agent for the WORST one per poll.
    # Running 4 LLM calls × N anomalies hammers the Groq rate limit fast.
    # The agent sees all correlated services in its context anyway, so
    # analysing the worst anomaly captures the full picture.
    if anomalies:
        # Sort by max z-score descending, pick the worst
        worst_service, worst_signal = max(
            anomalies,
            key=lambda x: x[1].get("metrics_snapshot", {}) and
                max((v.get("z_score", 0) for v in x[1].get("metrics_snapshot", {}).values()), default=0)
                if isinstance(x[1].get("metrics_snapshot"), dict) else 0
        )
        console.print(
            f"\n[bold red]  {len(anomalies)} anomaly signal(s) detected — "
            f"triggering agent for worst: {worst_service}[/bold red]"
        )
        try:
            from agent.agent_loop import run_agent
            run_agent(trigger="anomaly_detected", service_name=worst_service, signal=worst_signal)
        except Exception as e:
            console.print(f"[red]  Agent trigger failed: {e}[/red]")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    console.rule("[bold cyan]SRE Agent — Collector Started[/bold cyan]")
    console.print(f"  Polling {len(SERVICES)} services every {POLL_INTERVAL}s\n")

    # Graceful shutdown
    def _shutdown(sig, frame):
        console.print("\n[yellow]Shutting down collector...[/yellow]")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Run once immediately on startup
    poll_all_services()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        poll_all_services,
        trigger=IntervalTrigger(seconds=POLL_INTERVAL),
        id="poll_all",
        max_instances=1,        # never overlap if a poll takes long
        misfire_grace_time=10,
    )

    console.print(f"\n[green]Scheduler running. Next poll in {POLL_INTERVAL}s. Ctrl+C to stop.[/green]\n")
    scheduler.start()


if __name__ == "__main__":
    main()