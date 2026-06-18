"""
start.py

Single entry point that starts all SRE Agent components together.
Replaces running 4-5 separate terminals.

Usage:
    python start.py           # start everything
    python start.py --no-docker   # skip docker (if already running)
    python start.py --no-webhook  # skip webhook receiver

Components started:
    1. Docker (Prometheus + Grafana)   — background
    2. Mock services (6 FastAPI)       — background thread
    3. Collector + anomaly detection   — background thread
    4. Webhook receiver                — background thread
    5. Agent CLI                       — foreground (interactive)

Logs from background components are written to logs/ directory
so the CLI stays clean. Check logs/ if something seems wrong.

Stop everything: Ctrl+C
"""

import os
import sys

# MUST run before any other import in this process — several modules
# (collector/anomaly_detector.py, agent/agent_loop.py, main.py, etc.)
# print Unicode characters like ⚠ 📡 🤔 ✓ 📈 via Rich. On Windows, the
# default console encoding is cp1252 (a legacy codepage), which cannot
# represent most of those characters at all and raises
# UnicodeEncodeError the moment one is printed.
#
# This was silently killing more than just the print statement: when
# the crash happened inside collector.py's poll_all_services() (which
# runs as a scheduled APScheduler job, not in a try/except around every
# individual print), the UnicodeEncodeError propagated all the way up
# and made the ENTIRE scheduled poll job raise — meaning anomaly
# detection stopped running from that point in the session onward,
# disguised as a cosmetic console-encoding error.
#
# reconfigure(encoding="utf-8") forces stdout/stderr to UTF-8 regardless
# of the OS's default codepage, for THIS process and every thread it
# spawns (services, collector, webhook receiver, agent CLI all run as
# threads inside this one process — see _run_services/_run_collector/
# _run_webhook below). Available on Python 3.7+; safe no-op on
# platforms where the default is already UTF-8 (e.g. most Linux/macOS).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import time
import signal
import threading
import subprocess
import argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Log directory ─────────────────────────────────────────────────────────────

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def _log_path(name: str) -> Path:
    return LOG_DIR / f"{name}.log"


# ── Component runners ─────────────────────────────────────────────────────────

def _run_services(stop_event: threading.Event):
    """
    Run the unified mock services app in background.

    Was: services.service_runner.start_all() — 6 threads, 6 uvicorn
    servers, 6 ports (3001-3006).
    Now: services.app — ONE process, ONE port (default 8000), all 6
    services mounted under path prefixes (/payment, /cart, etc). See
    services/app.py's module docstring for the full rationale (Railway
    compatibility — one service maps to one port there).
    """
    log = open(_log_path("services"), "a", buffering=1, encoding="utf-8")
    log.write(f"\n--- Started {datetime.now()} ---\n")
    log.flush()

    try:
        from rich.console import Console as RichConsole
        import services.app as svc_module

        svc_module.console = RichConsole(file=log, highlight=False, width=120)

        svc_module.main()
    except Exception as e:
        log.write(f"ERROR: {e}\n")
        log.flush()
    finally:
        log.close()


def _run_collector(stop_event: threading.Event):
    """Run the collector + anomaly detector in background."""
    time.sleep(5)

    log = open(_log_path("collector"), "a", buffering=1, encoding="utf-8")
    log.write(f"\n--- Started {datetime.now()} ---\n")
    log.flush()

    try:
        import collector.collector as col_module
        import collector.anomaly_detector as anom_module
        from rich.console import Console as RichConsole

        # Redirect ALL console output to log file
        silent_console = RichConsole(file=log, highlight=False, width=120)
        col_module.console  = silent_console
        anom_module.console = silent_console

        from collector.collector import poll_all_services, POLL_INTERVAL
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        poll_all_services()

        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(
            poll_all_services,
            trigger=IntervalTrigger(seconds=POLL_INTERVAL),
            id="poll_all",
            max_instances=1,
            misfire_grace_time=10,
        )
        scheduler.start()
    except Exception as e:
        log.write(f"ERROR: {e}\n")
        log.flush()
    finally:
        log.close()


def _run_webhook(stop_event: threading.Event):
    """Run the Grafana webhook receiver in background."""
    time.sleep(3)

    log = open(_log_path("webhook"), "a", buffering=1, encoding="utf-8")
    log.write(f"\n--- Started {datetime.now()} ---\n")
    log.flush()

    try:
        import uvicorn
        from api.webhook_receiver import app
        port = int(os.getenv("WEBHOOK_PORT", "5001"))
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            access_log=False,
        )
    except Exception as e:
        log.write(f"ERROR: {e}\n")
        log.flush()
    finally:
        log.close()


def _start_docker(skip: bool):
    """Start Prometheus + Grafana via docker-compose."""
    if skip:
        return
    try:
        result = subprocess.run(
            ["docker-compose", "up", "-d"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            print("  Docker: Prometheus + Grafana started")
        else:
            print(f"  Docker: warning — {result.stderr.strip()[:100]}")
    except FileNotFoundError:
        print("  Docker: docker-compose not found — skipping")
    except subprocess.TimeoutExpired:
        print("  Docker: timeout — continuing anyway")
    except Exception as e:
        print(f"  Docker: {e} — skipping")


# ── Status display ────────────────────────────────────────────────────────────

def _wait_for_services(timeout: int = 15) -> bool:
    """Wait until the unified services app is reachable."""
    import httpx
    base_url = os.getenv("MOCK_SERVICES_BASE_URL", "http://localhost:8000")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/payment/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _print_status(webhook: bool):
    """Print what's running and where to find logs."""
    from rich.console import Console
    from rich.table import Table
    console = Console()

    table = Table(title="SRE Agent — Running Components", show_lines=True)
    table.add_column("Component",  style="cyan", width=22)
    table.add_column("Status",     width=10)
    table.add_column("Where",      width=30)
    table.add_column("Log file",   style="dim")

    # Check services (unified app — one port now, not 3001-3006)
    try:
        import httpx
        base_url = os.getenv("MOCK_SERVICES_BASE_URL", "http://localhost:8000")
        r = httpx.get(f"{base_url}/payment/health", timeout=1.0)
        svc_status = "[green]UP[/green]" if r.status_code == 200 else "[red]DOWN[/red]"
    except Exception:
        svc_status = "[yellow]starting...[/yellow]"

    # Check Prometheus
    try:
        import httpx
        r = httpx.get("http://localhost:9090/-/healthy", timeout=1.0)
        prom_status = "[green]UP[/green]" if r.status_code == 200 else "[red]DOWN[/red]"
    except Exception:
        prom_status = "[yellow]starting...[/yellow]"

    # Check webhook
    if webhook:
        try:
            import httpx
            r = httpx.get("http://localhost:5001/webhook/status", timeout=1.0)
            wh_status = "[green]UP[/green]" if r.status_code == 200 else "[red]DOWN[/red]"
        except Exception:
            wh_status = "[yellow]starting...[/yellow]"
    else:
        wh_status = "[dim]disabled[/dim]"

    base_url_display = os.getenv("MOCK_SERVICES_BASE_URL", "http://localhost:8000")
    table.add_row("Mock services (x6)",  svc_status,  base_url_display,           "logs/services.log")
    table.add_row("Collector",           "[green]UP[/green]", "background thread", "logs/collector.log")
    table.add_row("Prometheus",          prom_status, "localhost:9090",           "docker logs")
    table.add_row("Grafana",             "[dim]check browser[/dim]", "localhost:3000", "docker logs")
    table.add_row("Webhook receiver",    wh_status,   "localhost:5001",           "logs/webhook.log")
    table.add_row("Agent CLI",           "[green]UP[/green]", "this terminal",    "—")

    console.print(table)
    console.print(
        "[dim]  Collector running in background — "
        "agent will auto-trigger when anomaly detected.[/dim]"
    )
    console.print("[dim]  Watch logs/collector.log for collector output.[/dim]\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SRE Agent — start all components")
    parser.add_argument("--no-docker",   action="store_true", help="Skip docker-compose up")
    parser.add_argument("--no-webhook",  action="store_true", help="Skip webhook receiver")
    args = parser.parse_args()

    from rich.console import Console
    console = Console()
    console.rule("[bold cyan]SRE Agent — Starting All Components[/bold cyan]")

    stop_event = threading.Event()
    threads    = []

    # 1. Docker
    print("  Starting Docker (Prometheus + Grafana)...")
    _start_docker(skip=args.no_docker)

    # 2. Mock services
    print("  Starting mock services...")
    t = threading.Thread(target=_run_services, args=(stop_event,), daemon=True, name="services")
    t.start()
    threads.append(t)

    # 3. Collector
    print("  Starting collector...")
    t = threading.Thread(target=_run_collector, args=(stop_event,), daemon=True, name="collector")
    t.start()
    threads.append(t)

    # 4. Webhook receiver
    if not args.no_webhook:
        print("  Starting webhook receiver...")
        t = threading.Thread(target=_run_webhook, args=(stop_event,), daemon=True, name="webhook")
        t.start()
        threads.append(t)

    # Wait briefly for services to come up
    print("  Waiting for services to start...")
    _wait_for_services(timeout=15)

    # Show status
    print()
    _print_status(webhook=not args.no_webhook)

    # Graceful shutdown
    def _shutdown(sig, frame):
        console.print("\n[yellow]Shutting down all components...[/yellow]")
        stop_event.set()
        if not args.no_docker:
            try:
                subprocess.run(["docker-compose", "stop"], timeout=10, capture_output=True)
                console.print("  Docker stopped.")
            except Exception:
                pass
        console.print("[green]Goodbye.[/green]")
        os._exit(0)  # hard exit — avoids Windows readline teardown error

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 5. Agent CLI — runs in foreground
    from main import main as agent_main
    agent_main()


if __name__ == "__main__":
    main()