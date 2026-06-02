"""
services/simulate_incident.py

CLI tool to trigger incident scenarios on the mock services.
Use this to demo the agent's reasoning live.

Usage:
    python -m services.simulate_incident

Scenarios:
    1. Payment service gradual degradation  (catches predictive + RCA)
    2. Cascading failure payment → cart     (catches blast radius)
    3. Black Friday surge                   (catches load prediction)
    4. Gateway full outage                  (catches noise reduction)
    5. Recover all services
"""

import sys
import os
import time
import httpx
from rich.console import Console
from rich.prompt import Prompt
from rich import print as rprint

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

console = Console()

PORTS = {
    "payment_service":      3001,
    "cart_service":         3002,
    "notification_service": 3003,
    "auth_service":         3004,
    "inventory_service":    3005,
    "gateway_service":      3006,
}


def _post(service: str, endpoint: str, json: dict = None, params: dict = None):
    port = PORTS[service]
    url  = f"http://localhost:{port}/{endpoint}"
    try:
        r = httpx.post(url, json=json or {}, params=params or {}, timeout=3.0)
        return r.json()
    except Exception as e:
        console.print(f"[red]  Failed to reach {service}: {e}[/red]")
        return None


def scenario_gradual_degradation():
    """Payment service slowly degrades over time — triggers predictive alert."""
    console.print("\n[bold yellow]Scenario 1: Payment Service Gradual Degradation[/bold yellow]")
    console.print("  Degrading payment_service at intensity 0.6...")
    console.print("  Watch the collector detect the trend and trigger the agent.\n")
    _post("payment_service", "degrade", {"intensity": 0.6, "duration_seconds": 300})


def scenario_cascade():
    """Payment fails → cart degrades too — tests blast radius."""
    console.print("\n[bold red]Scenario 2: Cascading Failure (Payment → Cart)[/bold red]")
    console.print("  Triggering critical degradation on payment_service...")
    _post("payment_service", "degrade", {"intensity": 0.9, "duration_seconds": 300})
    time.sleep(5)
    console.print("  Triggering medium degradation on cart_service (downstream)...")
    _post("cart_service",    "degrade", {"intensity": 0.5, "duration_seconds": 300})
    console.print("  Both services now degrading — agent should identify shared dependency.\n")


def scenario_surge():
    """Simulate Black Friday surge — tests load prediction."""
    console.print("\n[bold magenta]Scenario 3: Black Friday Traffic Surge (5x)[/bold magenta]")
    console.print("  Surging all services at 5x normal traffic...")
    for svc in PORTS:
        _post(svc, "surge", params={"multiplier": 5.0})
    console.print("  All services under surge — agent should recommend scaling.\n")


def scenario_gateway_outage():
    """Gateway goes fully down — triggers noise reduction (all services alert)."""
    console.print("\n[bold red]Scenario 4: Gateway Full Outage[/bold red]")
    console.print("  Taking gateway_service fully down (intensity 1.0)...")
    console.print("  All services will start alerting — agent should group into 1 incident.\n")
    _post("gateway_service", "degrade", {"intensity": 1.0, "duration_seconds": 300})


def scenario_recover_all():
    """Recover all services to normal."""
    console.print("\n[bold green]Recovering all services...[/bold green]")
    for svc in PORTS:
        result = _post(svc, "recover")
        if result:
            console.print(f"  [green]✓[/green] {svc} recovered")
    console.print("")


def main():
    console.rule("[bold]SRE Agent — Incident Simulator[/bold]")
    console.print("Make sure services are running: [bold]python -m services.service_runner[/bold]\n")

    scenarios = {
        "1": ("Payment gradual degradation (predictive alert)",  scenario_gradual_degradation),
        "2": ("Cascading failure payment → cart (blast radius)", scenario_cascade),
        "3": ("Black Friday surge 5x (load prediction)",         scenario_surge),
        "4": ("Gateway full outage (noise reduction)",           scenario_gateway_outage),
        "5": ("Recover all services",                            scenario_recover_all),
    }

    for key, (desc, _) in scenarios.items():
        console.print(f"  [cyan]{key}[/cyan]. {desc}")

    console.print("")
    choice = Prompt.ask("Choose a scenario", choices=list(scenarios.keys()))
    _, fn  = scenarios[choice]
    fn()
    console.print("[dim]Collector will pick this up on next poll (up to 60s).[/dim]")


if __name__ == "__main__":
    main()
