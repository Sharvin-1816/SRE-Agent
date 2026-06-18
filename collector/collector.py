"""
collector/collector.py

Polls all 6 mock services every 60 seconds.
On each reading:
  1. Stores raw metrics to Supabase
  2. Runs anomaly detection (Z-score + trend)
  3. Updates baseline profiles incrementally
  4. Triggers agent only when warranted (cooldown + severity gating)

Domino prevention — two layers:
  Layer 1: Cooldown registry per service. After the agent analyses a
           service, that service is locked out for COOLDOWN_MINUTES.
           Only a severity escalation (score jump) or DOWN status
           overrides the cooldown.

  Layer 2: Minimum anomaly score to trigger the agent at all.
           Low z-scores (3.0-4.0) write to DB only — no LLM call.
           Score thresholds are configurable via .env.

Score thresholds:
  score < AGENT_MIN_SCORE          → DB write only, no agent
  score >= AGENT_MIN_SCORE         → agent trigger if not in cooldown
  score >= AGENT_ALWAYS_SCORE      → agent trigger always, overrides cooldown
  service DOWN                     → agent trigger always, overrides cooldown

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

from agent.logger import get_logger as _get_logger
_log = _get_logger("collector")
console = Console()

# ── Service registry ──────────────────────────────────────────────────────────

# Base URL of the unified mock-services process (services/app.py), which
# now serves all 6 services on ONE port under path prefixes instead of
# the old one-port-per-service model (localhost:3001..3006).
#
# Configurable via MOCK_SERVICES_BASE_URL so the upcoming Railway
# deployment step is just an environment variable change here, not
# another code edit — once the mock services move to Railway, this
# becomes that service's Railway-internal private hostname instead of
# localhost. Defaults to the correct value for local dev against
# `python -m services.app`.
_BASE_URL = os.getenv("MOCK_SERVICES_BASE_URL", "http://localhost:8000")

SERVICES = {
    "payment_service":      f"{_BASE_URL}/payment",
    "cart_service":         f"{_BASE_URL}/cart",
    "notification_service": f"{_BASE_URL}/notification",
    "auth_service":         f"{_BASE_URL}/auth",
    "inventory_service":    f"{_BASE_URL}/inventory",
    "gateway_service":      f"{_BASE_URL}/gateway",
}

POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
BASELINE_EVERY = 10

# ── Domino prevention config ──────────────────────────────────────────────────

# Minutes a service stays in cooldown after the agent analyses it
COOLDOWN_MINUTES = int(os.getenv("AGENT_COOLDOWN_MINUTES", "10"))

# Minimum anomaly score to trigger the agent at all
# Below this: write to DB only, no LLM call
AGENT_MIN_SCORE = float(os.getenv("AGENT_MIN_SCORE", "30"))

# Score at which the agent triggers regardless of cooldown
AGENT_ALWAYS_SCORE = float(os.getenv("AGENT_ALWAYS_SCORE", "80"))

# In-memory cooldown registry
# Key: service_name  Value: (last_triggered_at, last_score)
_cooldown: dict[str, tuple[datetime, float]] = {}


def _compute_anomaly_score(signal: dict) -> float:
    """
    Composite anomaly score 0-100 combining:
      - Z-score magnitude    (0-50 points)
      - Number of anomalous metrics (0-20 points)
      - Service DOWN flag    (0-30 points)

    This replaces the binary "anomaly yes/no" decision with a
    graduated scale so low z-scores don't trigger the full LLM pipeline.
    """
    if not signal:
        return 0.0

    snapshot = signal.get("metrics_snapshot", {})
    if not snapshot:
        return 0.0

    # Component 1 — z-score magnitude (0-50)
    z_scores = [
        v.get("z_score", 0)
        for v in snapshot.values()
        if isinstance(v, dict)
    ]
    max_z     = max(z_scores, default=0)
    # z=3 → 15pts, z=5 → 25pts, z=8 → 40pts, z=12+ → 50pts
    z_score_pts = min(50.0, (max_z / 12.0) * 50.0)

    # Component 2 — breadth (how many metrics are anomalous) (0-20)
    anomalous_count = sum(
        1 for v in snapshot.values()
        if isinstance(v, dict) and v.get("z_score", 0) >= 3.0
    )
    breadth_pts = min(20.0, anomalous_count * 5.0)

    # Component 3 — service DOWN (0-30)
    severity    = signal.get("severity", "low")
    down_pts    = 30.0 if severity == "critical" else 0.0

    score = round(z_score_pts + breadth_pts + down_pts, 1)
    return score


def _in_cooldown(service_name: str, current_score: float) -> tuple[bool, str]:
    """
    Check whether a service is in cooldown.

    Returns (suppressed: bool, reason: str).

    Cooldown is overridden when:
      - Score exceeds AGENT_ALWAYS_SCORE (genuine escalation)
      - Service is DOWN (severity = critical)
      - Score has increased by more than 20 points since last trigger
        (situation is getting worse, not stable)
    """
    entry = _cooldown.get(service_name)
    if not entry:
        return False, "no prior trigger"

    last_triggered, last_score = entry
    elapsed_minutes = (datetime.now(timezone.utc) - last_triggered).total_seconds() / 60

    if elapsed_minutes >= COOLDOWN_MINUTES:
        return False, f"cooldown expired ({elapsed_minutes:.1f} min ago)"

    if current_score >= AGENT_ALWAYS_SCORE:
        return False, f"score {current_score} exceeds always-trigger threshold ({AGENT_ALWAYS_SCORE})"

    if current_score - last_score >= 20:
        return False, f"severity escalated ({last_score} → {current_score})"

    remaining = COOLDOWN_MINUTES - elapsed_minutes
    return True, f"cooldown active — {remaining:.1f} min remaining (last score: {last_score})"


def _record_trigger(service_name: str, score: float):
    """Record that the agent was triggered for this service."""
    _cooldown[service_name] = (datetime.now(timezone.utc), score)


# ── Poll a single service ─────────────────────────────────────────────────────

def poll_service(service_name: str, base_url: str) -> dict:
    start = time.monotonic()
    try:
        resp       = httpx.get(f"{base_url}/metrics/json", timeout=5.0)
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
    if   0  <= hour < 6:  return "weekday_overnight",  0,  6
    elif 6  <= hour < 12: return "weekday_morning",    6, 12
    elif 12 <= hour < 18: return "weekday_afternoon", 12, 18
    else:                 return "weekday_evening",   18, 24


def update_baseline(service_name: str):
    now = datetime.now(timezone.utc)
    window_name, h_start, h_end = _get_time_window(now.hour)

    try:
        rows = get_historical_window(service_name, h_start, h_end, days_back=7)
    except Exception as e:
        console.print(f"[yellow]  Baseline update skipped for {service_name}: {e}[/yellow]")
        return

    if len(rows) < 10:
        return

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
    table = Table(
        title=f"[bold]Poll — {datetime.now().strftime('%H:%M:%S')}[/bold]",
        show_lines=False,
    )
    table.add_column("Service",    style="cyan", width=24)
    table.add_column("Status",     width=8)
    table.add_column("RT (ms)",    justify="right")
    table.add_column("Error %",    justify="right")
    table.add_column("Throughput", justify="right")
    table.add_column("CPU %",      justify="right")
    table.add_column("Memory %",   justify="right")

    for service_name, row in results:
        ok  = row["is_reachable"]
        rt  = row["response_time_ms"]
        er  = row["error_rate_pct"]
        tp  = row["throughput_rps"]
        cpu = row["cpu_pct"]
        mem = row["memory_pct"]

        # Show cooldown indicator in service name if active
        cd_entry = _cooldown.get(service_name)
        cd_indicator = ""
        if cd_entry:
            elapsed = (datetime.now(timezone.utc) - cd_entry[0]).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                remaining = COOLDOWN_MINUTES - elapsed
                cd_indicator = f" [dim](cd {remaining:.0f}m)[/dim]"

        table.add_row(
            service_name + cd_indicator,
            "[green]UP[/green]"   if ok  else "[red]DOWN[/red]",
            f"[red]{rt:.0f}[/red]"   if rt  > 1000 else f"{rt:.0f}",
            f"[red]{er:.1f}[/red]"   if er  > 5    else f"{er:.1f}",
            f"[yellow]{tp:.0f}[/yellow]" if tp < 50 else f"{tp:.0f}",
            f"[red]{cpu:.1f}[/red]"  if cpu > 85   else f"{cpu:.1f}",
            f"[red]{mem:.1f}[/red]"  if mem > 85   else f"{mem:.1f}",
        )

    console.print(table)


# ── Core poll job ─────────────────────────────────────────────────────────────

_poll_count = 0


def poll_all_services():
    """
    Called by APScheduler every POLL_INTERVAL seconds.
    Polls all services, writes metrics, runs anomaly detection,
    applies cooldown + severity gating before triggering agent.
    """
    global _poll_count
    _poll_count += 1

    results   = []
    anomalies = []   # (service_name, signal, score)

    for service_name, base_url in SERVICES.items():
        row = poll_service(service_name, base_url)

        try:
            insert_metric(row)
        except Exception as e:
            console.print(f"[red]  DB write failed for {service_name}: {e}[/red]")
            continue

        try:
            signal = check_for_anomalies(row)
            if signal:
                score = _compute_anomaly_score(signal)
                console.print(f"  [dim]Anomaly detected: {service_name} score={score}[/dim]")
                _log.info(
                    "Anomaly detected",
                    service=service_name,
                    score=score,
                    severity=signal.get("severity", "unknown"),
                    summary=signal.get("human_summary", "")[:200],
                )
                anomalies.append((service_name, signal, score))
        except Exception as e:
            # This except block's own error-reporting must never be able
            # to raise itself — if it did, the exception would propagate
            # straight past this function (an exception raised inside an
            # except block isn't caught by that same except), defeating
            # the entire point of this try/except: keeping one service's
            # anomaly-detection failure from killing the whole scheduled
            # poll job. This happened for real on 2026-06-18 — a Unicode
            # emoji in an anomaly message crashed console.print on
            # Windows' cp1252 terminal, which crashed THIS except block,
            # which crashed poll_all_services() entirely, silently
            # stopping anomaly detection for the rest of the session.
            # start.py now forces UTF-8 stdout so this specific cause
            # shouldn't recur, but this nested try/except remains as a
            # defense against ANY future console.print failure here,
            # for any reason.
            try:
                console.print(f"[yellow]  Anomaly check error for {service_name}: {e}[/yellow]")
            except Exception:
                pass
            try:
                _log.error("Anomaly check failed", service=service_name, error=str(e))
            except Exception:
                pass

        results.append((service_name, row))

        if _poll_count % BASELINE_EVERY == 0:
            try:
                update_baseline(service_name)
            except Exception as e:
                console.print(f"[yellow]  Baseline update failed for {service_name}: {e}[/yellow]")

    _print_poll_summary(results)

    if not anomalies:
        return

    # Sort by score descending
    anomalies.sort(key=lambda x: x[2], reverse=True)

    triggered   = False
    trigger_log = []

    for service_name, signal, score in anomalies:
        # Check if service is DOWN — always trigger, no gating
        is_down = not any(
            r[1].get("is_reachable", True)
            for r in results
            if r[0] == service_name
        )

        # Cooldown check — skip if recently triggered
        # unless service is DOWN or score is very high
        suppressed, reason = _in_cooldown(service_name, score)

        if suppressed and not is_down and score < AGENT_ALWAYS_SCORE:
            trigger_log.append(
                f"  [dim]{service_name}: suppressed ({reason})[/dim]"
            )
            continue

        # Trigger agent
        trigger_log.append(
            f"  [bold red]{service_name}: score={score} — triggering agent "
            f"({'DOWN' if is_down else reason})[/bold red]"
        )
        _record_trigger(service_name, score)
        _log.info(
            "Agent triggered",
            service=service_name,
            score=score,
            reason=reason,
            trigger="anomaly_detected",
        )

        try:
            from agent.agent_loop import run_agent
            run_agent(
                trigger="anomaly_detected",
                service_name=service_name,
                signal=signal,
            )
        except Exception as e:
            console.print(f"[red]  Agent trigger failed: {e}[/red]")

        triggered = True
        break

    if trigger_log:
        console.print(f"\n[bold]  {len(anomalies)} anomaly signal(s) detected:[/bold]")
        for line in trigger_log:
            console.print(line)

    if not triggered and anomalies:
        console.print(
            f"  [dim]All {len(anomalies)} signal(s) suppressed by cooldown[/dim]"
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    console.rule("[bold cyan]SRE Agent — Collector Started[/bold cyan]")
    console.print(f"  Polling {len(SERVICES)} services every {POLL_INTERVAL}s")
    console.print(f"  Agent cooldown: {COOLDOWN_MINUTES} min per service")
    console.print(f"  Min score to trigger agent: {AGENT_MIN_SCORE}")
    console.print(f"  Always-trigger score: {AGENT_ALWAYS_SCORE}\n")

    def _shutdown(sig, frame):
        console.print("\n[yellow]Shutting down collector...[/yellow]")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    poll_all_services()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        poll_all_services,
        trigger=IntervalTrigger(seconds=POLL_INTERVAL),
        id="poll_all",
        max_instances=1,
        misfire_grace_time=10,
    )

    console.print(
        f"[green]Scheduler running. Next poll in {POLL_INTERVAL}s. "
        f"Ctrl+C to stop.[/green]\n"
    )
    scheduler.start()


if __name__ == "__main__":
    main()