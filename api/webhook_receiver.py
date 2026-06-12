"""
api/webhook_receiver.py

FastAPI webhook receiver for Grafana alerts.
Implements Option C: immediate agent trigger with deduplication
window per service to avoid rate limit hammering.

Endpoint:
  POST /webhook/grafana    <- Grafana unified alerting
  GET  /webhook/status     <- health check + recent webhook log
  GET  /webhook/test       <- send a simulated alert for testing

Run:
  python -m api.webhook_receiver

Runs on port 5001 by default.

How to connect Grafana:
  1. Grafana -> Alerting -> Contact points -> Add contact point
  2. Type: Webhook
  3. URL: http://host.docker.internal:5001/webhook/grafana
  4. Save and test

Testing without a real alert:
  curl http://localhost:5001/webhook/test
"""

import os
import sys
import threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from collections import deque
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

from api.parsers.grafana   import parse as parse_grafana
from api.parsers.normaliser import make_internal_alert
from db.database            import insert_alert, insert_anomaly
from agent.logger           import get_logger as _get_logger

console = Console()
_log = _get_logger("webhook")

app = FastAPI(title="SRE Agent Webhook Receiver")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Deduplication window ──────────────────────────────────────────────────────

DEDUP_WINDOW_MINUTES = int(os.getenv("WEBHOOK_DEDUP_MINUTES", "5"))
_last_triggered: dict[str, datetime] = {}
_recent_log: deque = deque(maxlen=50)


def _should_trigger_agent(service_name: str) -> tuple[bool, str]:
    now  = datetime.now(timezone.utc)
    last = _last_triggered.get(service_name)

    if last is None:
        return True, "first alert for this service"

    elapsed = (now - last).total_seconds() / 60
    if elapsed >= DEDUP_WINDOW_MINUTES:
        return True, f"last trigger was {elapsed:.1f} min ago"

    return False, f"deduplicated — same service triggered {elapsed:.1f} min ago"


def _record_trigger(service_name: str):
    _last_triggered[service_name] = datetime.now(timezone.utc)


def _log(source: str, alerts: list, triggered: bool, reason: str):
    _recent_log.appendleft({
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source":      source,
        "alerts":      len(alerts),
        "triggered":   triggered,
        "reason":      reason,
        "services":    [a["service_name"] for a in alerts],
    })


# ── Core processing ───────────────────────────────────────────────────────────

def _process_alerts(alerts: list[dict], source: str) -> dict:
    if not alerts:
        return {
            "received":  0,
            "triggered": False,
            "reason":    "no actionable alerts parsed",
        }

    # Write all alerts to Supabase regardless of deduplication
    for alert in alerts:
        try:
            insert_alert({
                "service_name": alert["service_name"],
                "anomaly_id":   None,
                "triggered_at": alert["received_at"],
                "metric":       alert["metric"],
                "severity":     alert["severity"],
                "message":      f"[{source.upper()}] {alert['summary']}",
            })
        except Exception as e:
            console.print(f"[yellow]  DB write failed: {e}[/yellow]")

    # Pick the worst alert to drive the agent
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    worst        = max(alerts, key=lambda a: severity_rank.get(a["severity"], 0))
    service_name = worst["service_name"]

    console.print(
        f"\n[bold]Webhook received from {source.upper()}[/bold] — "
        f"{len(alerts)} alert(s) | service: {service_name} | "
        f"severity: {worst['severity']}"
    )
    console.print(f"  {worst['summary']}")

    _log.info(
        "Webhook received",
        source=source,
        service=service_name,
        severity=worst["severity"],
        alert_count=len(alerts),
        summary=worst["summary"][:200],
    )

    should_trigger, reason = _should_trigger_agent(service_name)

    if should_trigger:
        _record_trigger(service_name)
        console.print(f"  Agent trigger: YES ({reason})")
        _trigger_agent(worst, source)
    else:
        console.print(f"  Agent trigger: NO ({reason})")

    _log(source, alerts, should_trigger, reason)

    return {
        "received":  len(alerts),
        "triggered": should_trigger,
        "reason":    reason,
        "service":   service_name,
        "severity":  worst["severity"],
    }


def _trigger_agent(alert: dict, source: str):
    """
    Build a synthetic LLM signal from the webhook alert and
    trigger the agent in a background thread so the webhook
    response returns immediately.
    """
    try:
        insert_anomaly({
            "service_name":        alert["service_name"],
            "detected_at":         alert["received_at"],
            "max_z_score":         99.0,
            "severity":            alert["severity"],
            "anomalies": [{
                "metric":        alert["metric"],
                "current_value": alert.get("current_value"),
                "z_score":       99.0,
                "severity":      alert["severity"],
            }],
            "trend":               {},
            "correlated_services": [],
            "processed_by_agent":  False,
        })
    except Exception as e:
        console.print(f"[yellow]  Anomaly event write failed: {e}[/yellow]")

    signal = {
        "service_name":  alert["service_name"],
        "severity":      alert["severity"],
        "human_summary": (
            f"External alert received from {source.upper()}: "
            f"{alert['summary']}. "
            f"Metric affected: {alert['metric']}."
            + (f" Current value: {alert['current_value']}." if alert.get("current_value") else "")
            + (f" Threshold: {alert['threshold']}." if alert.get("threshold") else "")
        ),
        "metrics_snapshot": {
            alert["metric"]: {
                "now":          alert.get("current_value", "unknown"),
                "normal_range": f"below {alert.get('threshold', 'threshold')}",
                "z_score":      99.0,
                "severity":     alert["severity"],
            }
        },
        "trend_summary":       "Externally reported via Grafana alert.",
        "hypothesis_hints":    [f"Alert fired by Grafana rule on {alert['metric']}"],
        "correlated_services": [],
    }

    def _run():
        try:
            from agent.agent_loop import run_agent
            run_agent(
                trigger=f"webhook_{source}",
                service_name=alert["service_name"],
                signal=signal,
            )
        except Exception as e:
            console.print(f"[red]  Agent run failed from webhook: {e}[/red]")

    threading.Thread(target=_run, daemon=True, name="webhook-agent").start()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/webhook/grafana")
async def webhook_grafana(request: Request):
    """Receive Grafana unified alerting webhook."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    alerts = parse_grafana(payload)
    result = _process_alerts(alerts, "grafana")
    return JSONResponse(content=result)


@app.get("/webhook/status")
def webhook_status():
    """Health check and recent webhook activity."""
    return {
        "status":            "ok",
        "service":           "SRE Agent Webhook Receiver",
        "dedup_window_mins": DEDUP_WINDOW_MINUTES,
        "active_cooldowns": {
            svc: f"{((datetime.now(timezone.utc) - ts).total_seconds() / 60):.1f} min ago"
            for svc, ts in _last_triggered.items()
        },
        "recent_webhooks": list(_recent_log),
    }


@app.get("/webhook/test")
def webhook_test():
    """
    Simulate a Grafana alert firing — no real Grafana alert needed.
    Tests the full pipeline: parse -> DB -> agent trigger.
    """
    payload = {
        "status":   "firing",
        "receiver": "sre-agent",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "HighLatency",
                "service":   "payment_service",
                "severity":  "critical",
            },
            "annotations": {
                "summary":     "p95 latency is 3800ms, threshold is 2500ms",
                "description": "Threshold breached: 2500ms. Current: 3800ms",
            },
            "startsAt": datetime.now(timezone.utc).isoformat(),
            "values":   {"B": 3800},
        }],
    }
    alerts = parse_grafana(payload)
    result = _process_alerts(alerts, "grafana")
    return JSONResponse(content={"test": True, **result})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("WEBHOOK_PORT", "5001"))
    console.print(f"\n[bold cyan]SRE Agent Webhook Receiver[/bold cyan]")
    console.print(f"  Listening on port {port}\n")
    console.print(f"  POST http://localhost:{port}/webhook/grafana")
    console.print(f"  GET  http://localhost:{port}/webhook/status")
    console.print(f"  GET  http://localhost:{port}/webhook/test\n")
    console.print(f"  Connect Grafana contact point to:")
    console.print(f"  http://host.docker.internal:{port}/webhook/grafana\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")