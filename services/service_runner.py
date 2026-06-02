"""
services/service_runner.py

Starts all 6 mock services simultaneously, each on its own port.
Each service runs in a separate thread using uvicorn.

Run:
    python -m services.service_runner

Ports:
    payment_service      → http://localhost:3001
    cart_service         → http://localhost:3002
    notification_service → http://localhost:3003
    auth_service         → http://localhost:3004
    inventory_service    → http://localhost:3005
    gateway_service      → http://localhost:3006

Manual controls (for demo / simulate incident):
    curl -X POST http://localhost:3001/degrade -H "Content-Type: application/json" \
         -d '{"intensity": 0.8, "duration_seconds": 120}'

    curl -X POST http://localhost:3001/recover
    curl -X POST http://localhost:3001/surge?multiplier=5.0
"""

import sys
import os
import signal
import threading
import uvicorn
from rich.console import Console
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.definitions import ALL_SERVICES

console = Console()
_threads: list[threading.Thread] = []


def _run_service(name: str, app, port: int):
    """Run a single uvicorn server. Called in a thread."""
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="error",   # suppress uvicorn access logs — collector output is cleaner
    )
    server = uvicorn.Server(config)
    server.run()


def start_all():
    console.rule("[bold cyan]SRE Agent — Mock Services[/bold cyan]")

    table = Table(show_header=True)
    table.add_column("Service",  style="cyan")
    table.add_column("Port",     style="green")
    table.add_column("Endpoints")

    for name, (svc, port) in ALL_SERVICES.items():
        table.add_row(
            name,
            str(port),
            f"GET /health  GET /metrics  POST /degrade  POST /recover  POST /surge"
        )
        t = threading.Thread(
            target=_run_service,
            args=(name, svc.app, port),
            daemon=True,
            name=f"svc-{name}",
        )
        _threads.append(t)
        t.start()

    console.print(table)
    console.print("\n[green]All 6 services running. Ctrl+C to stop.[/green]")
    console.print("[dim]Tip: use POST /degrade or POST /surge to simulate incidents[/dim]\n")

    # Keep main thread alive
    def _shutdown(sig, frame):
        console.print("\n[yellow]Shutting down services...[/yellow]")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    for t in _threads:
        t.join()


if __name__ == "__main__":
    start_all()
