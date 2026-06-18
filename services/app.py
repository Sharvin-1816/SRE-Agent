"""
services/app.py

Single FastAPI process exposing all 6 mock microservices as sub-apps
mounted under their own path prefix, on ONE port — replaces the old
six-thread/six-port model from service_runner.py.

Why this exists (Railway compatibility):
  Railway's networking model (private networking between services,
  "Generate Domain" for public access) is built around one service =
  one process = one port. The original service_runner.py ran 6 separate
  uvicorn.Server instances on 6 threads inside one process, each bound
  to its own port (3001-3006) — that pattern has no clean equivalent in
  Railway, which expects a single listening port per service.

  This file solves it by mounting each BaseService instance's own
  FastAPI app (built entirely inside base_service.py, completely
  unchanged) under a URL prefix instead of a distinct port:

      OLD (local, port-based):
        http://localhost:3001/health   (payment_service)
        http://localhost:3002/health   (cart_service)

      NEW (path-based, one port):
        http://localhost:8000/payment/health
        http://localhost:8000/cart/health

  base_service.py and definitions.py are COMPLETELY UNCHANGED — every
  route inside BaseService is defined relative ("/health", not
  "/payment/health"), so FastAPI's app.mount(prefix, sub_app) correctly
  rewrites the effective path for free. No internal logic needed to
  know or care that it's mounted under a prefix.

Local development:
  python -m services.app
  Listens on PORT env var, defaulting to 8000.
  All 6 services reachable at:
    http://localhost:8000/payment/...
    http://localhost:8000/cart/...
    http://localhost:8000/notification/...
    http://localhost:8000/auth/...
    http://localhost:8000/inventory/...
    http://localhost:8000/gateway/...

Railway:
  This becomes ONE Railway service. Set the service's start command to
  `python -m services.app` (or let Railpack/Procfile detect it — see
  Procfile in repo root). Railway sets PORT automatically; this file
  reads it via os.getenv("PORT", "8000") so no hardcoded port is needed.

Manual controls (for demo / simulate incident) — same endpoints, new prefix:
    curl -X POST http://localhost:8000/payment/degrade \
         -H "Content-Type: application/json" \
         -d '{"intensity": 0.8, "duration_seconds": 120}'

    curl -X POST http://localhost:8000/payment/recover
    curl -X POST "http://localhost:8000/payment/surge?multiplier=5.0"
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from fastapi import FastAPI
from rich.console import Console
from rich.table import Table

from services.definitions import ALL_SERVICES

console = Console()

# The path prefix each service is mounted under. Derived directly from
# the service name by stripping the "_service" suffix, so this requires
# zero manual mapping to keep in sync — add a 7th service to
# definitions.py's ALL_SERVICES and it's automatically mounted here too.
def _prefix_for(service_name: str) -> str:
    return "/" + service_name.replace("_service", "")


app = FastAPI(title="SRE Agent — Mock Services (unified)")

# Build the prefix -> port mapping once, used both for mounting and for
# the startup table below (so the displayed table can't drift out of
# sync with what's actually mounted — it's reading the same source).
_mounted: list[tuple[str, str, int]] = []  # (service_name, prefix, original_port)

for name, (svc, original_port) in ALL_SERVICES.items():
    prefix = _prefix_for(name)
    app.mount(prefix, svc.app)
    _mounted.append((name, prefix, original_port))


@app.get("/")
def root():
    """
    Top-level health/index — NOT one of the 6 mock services, just confirms
    the unified process itself is up and lists what's mounted where.
    Useful as Railway's health-check target for this service.
    """
    return {
        "status":  "ok",
        "service": "SRE Agent unified mock services",
        "mounted": [
            {"name": name, "prefix": prefix, "former_local_port": port}
            for name, prefix, port in _mounted
        ],
    }


def _print_startup_table():
    console.rule("[bold cyan]SRE Agent — Mock Services (unified, single port)[/bold cyan]")

    table = Table(show_header=True)
    table.add_column("Service",  style="cyan")
    table.add_column("Path prefix", style="green")
    table.add_column("Was local port (now path-based)")

    for name, prefix, port in _mounted:
        table.add_row(name, prefix, str(port))

    console.print(table)
    console.print(
        "\n[green]All 6 services mounted on one port. "
        "Ctrl+C to stop.[/green]"
    )
    console.print(
        "[dim]Tip: e.g. POST /payment/degrade or POST /payment/surge "
        "to simulate incidents[/dim]\n"
    )


def main():
    _print_startup_table()
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="error")


if __name__ == "__main__":
    main()
