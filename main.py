"""
main.py

CLI entry point for the SRE Agent.
Run this to interact with the agent directly.

Usage:
    python main.py

Commands available in the CLI:
    context   — add free-text operator context
    contexts  — view all saved context entries
    query     — ask a natural language health question
    predict   — run load prediction
    blast     — run blast radius for a service
    alerts    — run alert noise reduction
    simulate  — trigger an incident scenario
    help      — show commands
    exit      — quit
"""

import sys
import os
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.database import (
    add_user_context, list_context_entries,
    delete_context_entry, get_latest_metric_per_service
)
from agent.agent_loop import (
    run_health_query, run_load_prediction,
    run_blast_radius, run_alert_grouping
)

console = Console()

SERVICES = [
    "payment_service", "cart_service", "notification_service",
    "auth_service", "inventory_service", "gateway_service"
]


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_add_context():
    """Let user type free-text context entries."""
    console.print(Panel(
        "Type any information relevant to your services.\n"
        "Examples:\n"
        "  • There will be a power outage on 3rd March from 2-6 AM\n"
        "  • New movie releasing on 9th April, expecting massive traffic\n"
        "  • Flash sale every Friday 6-9 PM, expect 3-4x load\n"
        "  • Deployment of v3.0 tonight at 11 PM\n\n"
        "Press Enter twice or type 'done' to finish.",
        title="[bold cyan]Add Operator Context[/bold cyan]",
        border_style="cyan",
    ))

    lines = []
    while True:
        line = input("  > ").strip()
        if line.lower() == "done" or line == "":
            if lines:
                break
            elif line == "":
                console.print("[yellow]  Nothing entered. Type your context or 'done' to cancel.[/yellow]")
        else:
            lines.append(line)

    if lines:
        text = " ".join(lines)
        entry = add_user_context(text)
        console.print(f"[green]  ✓ Saved:[/green] {text}")
    else:
        console.print("[yellow]  Cancelled.[/yellow]")


def cmd_view_contexts():
    """Show all active context entries."""
    entries = list_context_entries()
    if not entries:
        console.print("[yellow]  No context entries saved yet.[/yellow]")
        return

    table = Table(title="Active Operator Context", show_lines=True)
    table.add_column("#",       width=4)
    table.add_column("Added",   width=12)
    table.add_column("Source",  width=16)
    table.add_column("Context", style="cyan")

    for i, e in enumerate(entries, 1):
        table.add_row(
            str(i),
            e["added_at"][:10],
            e["source"],
            e["value"],
        )

    console.print(table)

    # Offer delete
    sys.stdout.write("\nDelete an entry? Enter number or press Enter to skip: ")
    sys.stdout.flush()
    delete = input().strip()
    if delete.isdigit():
        idx = int(delete) - 1
        if 0 <= idx < len(entries):
            delete_context_entry(entries[idx]["key"])
            console.print(f"[green]  ✓ Deleted entry {delete}[/green]")


def cmd_health_query():
    """Natural language health query."""
    console.print("[dim]Examples: 'Which services were unstable this weekend?'[/dim]")
    console.print("[dim]          'What failed yesterday?'[/dim]")
    console.print("[dim]          'Show me last 24 hours'[/dim]\n")
    sys.stdout.write("Your question: ")
    sys.stdout.flush()
    question = input()
    if question.strip():
        run_health_query(question)


def cmd_load_prediction():
    """Load prediction — all services or specific one."""
    console.print(f"[dim]Services: {', '.join(SERVICES)}[/dim]")
    sys.stdout.write("Service name (or press Enter for all services): ")
    sys.stdout.flush()
    svc = input()
    service = svc.strip() if svc.strip() in SERVICES else None
    if svc.strip() and not service:
        console.print(f"[yellow]  Unknown service '{svc}'. Running for all services.[/yellow]")
    run_load_prediction(service)


def cmd_blast_radius():
    """Blast radius for a specific service."""
    console.print(f"[dim]Services: {', '.join(SERVICES)}[/dim]")
    sys.stdout.write("Which service is failing: ")
    sys.stdout.flush()
    svc = input()
    if svc.strip() in SERVICES:
        run_blast_radius(svc.strip())
    else:
        console.print(f"[red]  Unknown service: {svc}[/red]")


def cmd_alert_noise():
    """Run alert noise reduction."""
    run_alert_grouping()


def cmd_view_memory():
    """View all stored long term memory patterns."""
    from rich.table import Table
    SERVICES = [
        "payment_service", "cart_service", "notification_service",
        "auth_service", "inventory_service", "gateway_service"
    ]

    try:
        from db.database import db
        result = (
            db().table("agent_memory_patterns")
            .select("*")
            .order("occurrence_count", desc=True)
            .execute()
        )
        patterns = result.data
    except Exception as e:
        console.print(f"[red]  Error fetching memory: {e}[/red]")
        return

    if not patterns:
        console.print("[yellow]  No memory patterns stored yet. Run some agent analyses first.[/yellow]")
        return

    table = Table(title="Long Term Memory Patterns", show_lines=True)
    table.add_column("Service",     style="cyan", width=22)
    table.add_column("Pattern",     width=24)
    table.add_column("Seen",        width=6,  justify="right")
    table.add_column("Root cause",  width=30)
    table.add_column("Resolution",  width=30)
    table.add_column("Outcome",     width=12)
    table.add_column("Last seen",   width=12)

    for p in patterns:
        correct = p.get("prediction_was_correct")
        if correct is True:
            outcome_str = "correct"
        elif correct is False:
            outcome_str = "incorrect"
        else:
            outcome_str = p.get("outcome", "unknown")

        table.add_row(
            p.get("service_name", ""),
            p.get("pattern_type", ""),
            str(p.get("occurrence_count", 1)),
            (p.get("root_cause") or "")[:30],
            (p.get("resolution") or "")[:30],
            outcome_str,
            p.get("last_seen", "")[:10],
        )

    console.print(table)


def cmd_status():
    """Quick status of all services."""
    # Show which data source is active — suppress any adapter console output
    try:
        from agent.prometheus_adapter import is_available as prom_ok
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            prom_running = prom_ok()
        if prom_running:
            console.print("  [cyan]Data source: Prometheus (p95/p99 available)[/cyan]")
        else:
            console.print("  [yellow]Data source: Supabase (Prometheus unavailable — run docker-compose up -d prometheus)[/yellow]")
    except Exception:
        console.print("  [yellow]Data source: Supabase[/yellow]")

    # OTel / Tempo status
    try:
        from agent.trace_adapter import is_available as tempo_ok
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            running = tempo_ok()
        if running:
            console.print("  [cyan]Tracing: Grafana Tempo active (distributed traces available)[/cyan]")
        else:
            console.print("  [dim]Tracing: Grafana Tempo offline (run docker-compose up -d tempo)[/dim]")
    except Exception:
        pass
    if not rows:
        console.print("[yellow]  No metrics yet. Is the collector running?[/yellow]")
        return

    table = Table(title="Current Service Status", show_lines=False)
    table.add_column("Service",    style="cyan", width=24)
    table.add_column("Status",     width=8)
    table.add_column("RT (ms)",    justify="right")
    table.add_column("Error %",    justify="right")
    table.add_column("Throughput", justify="right")
    table.add_column("CPU %",      justify="right")
    table.add_column("Last seen",  width=12)

    for r in rows:
        ok  = r.get("is_reachable", True)
        rt  = r.get("response_time_ms", 0)
        er  = r.get("error_rate_pct",   0)
        tp  = r.get("throughput_rps",   0)
        cpu = r.get("cpu_pct",          0)

        status_str = "[green]UP[/green]"   if ok  else "[red]DOWN[/red]"
        rt_str     = f"[red]{rt:.0f}[/red]"  if rt  > 1000 else f"{rt:.0f}"
        er_str     = f"[red]{er:.1f}[/red]"  if er  > 5    else f"{er:.1f}"
        tp_str     = f"[yellow]{tp:.0f}[/yellow]" if tp < 50 else f"{tp:.0f}"
        cpu_str    = f"[red]{cpu:.1f}[/red]" if cpu > 85   else f"{cpu:.1f}"
        ts         = r.get("timestamp", "")[:16] if r.get("timestamp") else "?"

        table.add_row(
            r["service_name"], status_str,
            rt_str, er_str, tp_str, cpu_str, ts
        )

    console.print(table)


def cmd_webhooks():
    """Show webhook receiver status and recent activity."""
    try:
        import httpx
        port = int(os.getenv("WEBHOOK_PORT", "5001"))
        resp = httpx.get(f"http://localhost:{port}/webhook/status", timeout=3.0)
        data = resp.json()

        console.print(Panel(
            f"[bold]Webhook Receiver[/bold] — port {port}\n"
            f"Grafana contact point URL: http://host.docker.internal:{port}/webhook/grafana\n"
            f"Dedup window: {data.get('dedup_window_mins', 5)} minutes\n\n"
            f"[bold]Active cooldowns:[/bold]\n" +
            (
                "\n".join(
                    f"  {svc}: last triggered {ago}"
                    for svc, ago in data.get("active_cooldowns", {}).items()
                ) or "  None"
            ) +
            f"\n\n[bold]Recent webhooks:[/bold]\n" +
            (
                "\n".join(
                    f"  [{w['received_at'][:16]}] {w['source'].upper()} — "
                    f"{w['alerts']} alert(s) on {', '.join(w['services'])} — "
                    f"{'triggered' if w['triggered'] else 'deduplicated'}"
                    for w in data.get("recent_webhooks", [])[:5]
                ) or "  No webhooks received yet"
            ),
            title="[bold cyan]Webhook Receiver Status[/bold cyan]",
            border_style="cyan",
        ))
    except Exception:
        console.print(
            "[yellow]  Webhook receiver is not running.[/yellow]\n"
            "[dim]  Start it: python -m api.webhook_receiver[/dim]\n\n"
            "[dim]  Then in Grafana:[/dim]\n"
            "[dim]    Alerting -> Contact points -> Add contact point[/dim]\n"
            "[dim]    Type: Webhook[/dim]\n"
            "[dim]    URL: http://host.docker.internal:5001/webhook/grafana[/dim]\n\n"
            "[dim]  Test without real alert:[/dim]\n"
            "[dim]    curl http://localhost:5001/webhook/test[/dim]"
        )


def cmd_help():
    console.print(Panel(
        "[bold cyan]context[/bold cyan]   — Add free-text operator context (events, deployments, outages)\n"
        "[bold cyan]contexts[/bold cyan]  — View and manage saved context entries\n"
        "[bold cyan]status[/bold cyan]    — Quick view of all service health right now\n"
        "[bold cyan]query[/bold cyan]     — Ask a natural language health question\n"
        "[bold cyan]predict[/bold cyan]   — Run load + capacity prediction\n"
        "[bold cyan]blast[/bold cyan]     — Estimate blast radius if a service fails\n"
        "[bold cyan]alerts[/bold cyan]    — Run alert noise reduction on current alerts\n"
        "[bold cyan]simulate[/bold cyan]  — Trigger an incident scenario for demo\n"
        "[bold cyan]memory[/bold cyan]    — View all stored long term memory patterns\n"
        "[bold cyan]webhooks[/bold cyan]  — Show webhook receiver status and recent activity\n"
        "[bold cyan]logs[/bold cyan]      — View recent logs from all components\n"
        "[bold cyan]help[/bold cyan]      — Show this menu\n"
        "[bold cyan]exit[/bold cyan]      — Quit",
        title="[bold]Available Commands[/bold]",
        border_style="cyan",
    ))


# ── Main REPL ─────────────────────────────────────────────────────────────────

def cmd_logs():
    """View recent logs from all components via Loki or local files."""
    try:
        import httpx
        # Try Loki first
        resp = httpx.get(
            "http://localhost:3100/loki/api/v1/query_range",
            params={
                "query": '{job=~"sre_.*"}',
                "limit": 50,
                "start": str(int((__import__("time").time() - 3600) * 1e9)),
                "end":   str(int(__import__("time").time() * 1e9)),
            },
            timeout=3.0,
        )
        if resp.status_code == 200:
            data   = resp.json()
            result = data.get("data", {}).get("result", [])
            entries = []
            for stream in result:
                component = stream.get("stream", {}).get("component", "unknown")
                for ts, line in stream.get("values", []):
                    try:
                        import json as _json
                        entry = _json.loads(line)
                        entries.append((ts, component, entry))
                    except Exception:
                        entries.append((ts, component, {"message": line, "level": "info"}))

            entries.sort(key=lambda x: x[0], reverse=True)

            from rich.table import Table
            table = Table(title="Recent Logs (via Loki)", show_lines=False)
            table.add_column("Time",      width=10, style="dim")
            table.add_column("Component", width=12, style="cyan")
            table.add_column("Level",     width=8)
            table.add_column("Message")

            for _, component, entry in entries[:30]:
                ts_str  = entry.get("timestamp", "")[:16]
                level   = entry.get("level", "info")
                message = entry.get("message", "")
                level_style = {
                    "error":   "[red]error[/red]",
                    "warning": "[yellow]warn[/yellow]",
                    "info":    "[green]info[/green]",
                    "debug":   "[dim]debug[/dim]",
                }.get(level, level)
                table.add_row(ts_str, component, level_style, message)

            console.print(table)
            console.print("[dim]  Full logs: http://localhost:3000 → Explore → Loki[/dim]")
            return
    except Exception:
        pass

    # Fall back to local log files
    console.print("[yellow]  Loki not available — showing local log files[/yellow]")
    log_dir = __import__("pathlib").Path("logs")
    if not log_dir.exists():
        console.print("[dim]  No logs directory found.[/dim]")
        return
    for f in sorted(log_dir.glob("*.log")):
        lines = f.read_text(encoding="utf-8", errors="replace").strip().split("\n")
        last  = lines[-10:] if len(lines) > 10 else lines
        console.print(f"\n[cyan]{f.name}[/cyan] (last {len(last)} lines):")
        for line in last:
            console.print(f"  [dim]{line[:120]}[/dim]")


def cmd_simulate():
    """Launch the incident simulator."""
    console.print("[dim]Launching incident simulator...[/dim]")
    os.system("python -m services.simulate_incident")


COMMANDS = {
    "context":   cmd_add_context,
    "contexts":  cmd_view_contexts,
    "status":    cmd_status,
    "query":     cmd_health_query,
    "predict":   cmd_load_prediction,
    "blast":     cmd_blast_radius,
    "alerts":    cmd_alert_noise,
    "simulate":  cmd_simulate,
    "memory":    cmd_view_memory,
    "webhooks":  cmd_webhooks,
    "logs":      cmd_logs,
    "help":      cmd_help,
}


def main():
    console.rule("[bold cyan]SRE Agent — Operational Intelligence[/bold cyan]")
    console.print(
        "Agent ready. Type [bold cyan]help[/bold cyan] for commands.\n"
    )

    while True:
        try:
            sys.stdout.write("\nagent: ")
            sys.stdout.flush()
            cmd = input().strip().lower()

            if cmd in ("exit", "quit", "q"):
                console.print("[yellow]Goodbye.[/yellow]")
                break
            elif cmd in COMMANDS:
                COMMANDS[cmd]()
            elif cmd == "":
                continue
            else:
                console.print(f"[red]  Unknown command: '{cmd}'. Type 'help' for options.[/red]")

        except KeyboardInterrupt:
            console.print("\n[yellow]Goodbye.[/yellow]")
            break
        except EOFError:
            break
        except Exception as e:
            console.print(f"[bold red]  Error: {e}[/bold red]")


if __name__ == "__main__":
    main()