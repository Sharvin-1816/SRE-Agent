"""
agent/agent_loop.py

The core agentic loop. Called whenever:
  - Collector detects an anomaly
  - User runs a health query
  - User requests blast radius
  - User requests load prediction
  - Scheduled every 5 minutes for proactive check

Loop:
  1. OBSERVE   — build context package
  2. REASON    — call LLM with context
  3. GAPS?     — if confidence < threshold, ask user via CLI
  4. CONCLUDE  — store and display final output
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich import print as rprint
from dotenv import load_dotenv

load_dotenv()


def is_interactive() -> bool:
    """
    True only when a real human is at a real terminal that can answer a
    Prompt.ask() call. False in every other case this function runs in:

    - start.py --headless (no CLI at all, sys.stdin isn't a real tty)
    - webhook-triggered runs (agent_loop runs inside a background thread
      spawned by api/webhook_receiver.py's _trigger_agent(), which has no
      controlling terminal of its own regardless of whether some OTHER
      thread in the same process happens to be running the interactive CLI)
    - dashboard-triggered jobs (api/dashboard_api.py's job system runs
      agent_loop functions in worker threads, same situation)
    - the collector's own automatic anomaly-triggered runs

    Before this existed, _handle_context_gap()'s Prompt.ask() blocked
    forever in every one of these cases — there was no human able to type
    a response, so the LLM call that started the whole analysis just
    never returned. This surfaced as RCA/prediction jobs that appeared to
    take "very very long" and then silently completed nothing on the
    dashboard, because the work was actually stuck on a blocking stdin
    read deep inside agent_loop.py, invisible to the dashboard's job
    polling and invisible to whoever triggered it unless they happened to
    be watching the exact terminal where start.py's CLI (if any) was
    running.
    """
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


from agent.llm_adapter import ask_llm, parse_json_response
from agent.prompts import (
    RCA_SYSTEM, PREDICT_SYSTEM, LOAD_SYSTEM,
    ALERT_GROUPING_SYSTEM, HEALTH_QUERY_SYSTEM, BLAST_RADIUS_SYSTEM
)
from agent.context_builder import (
    build_for_rca, build_for_prediction, build_for_load_prediction,
    build_for_alert_grouping, build_for_health_query, build_for_blast_radius
)
from agent.memory import (
    build_memory_context, display_used_memories,
    extract_and_store_pattern, build_signal_text, store_signal_embedding,
)
from agent.logger import get_logger as _get_logger
from db.database import (
    insert_agent_output, get_ungrouped_alerts,
    mark_alerts_grouped, insert_incident, add_user_context
)

console = Console()
CONFIDENCE_THRESHOLD = int(os.getenv("AGENT_CONFIDENCE_THRESHOLD", "75"))
_log = _get_logger("agent")


def _get_time_of_day(hour: int) -> str:
    if   0  <= hour < 6:  return "overnight"
    elif 6  <= hour < 12: return "morning"
    elif 12 <= hour < 18: return "afternoon"
    else:                 return "evening"


# ── Output display ────────────────────────────────────────────────────────────

def _display_rca(result: dict, service_name: str):
    console.print(Panel(
        f"[bold red]ROOT CAUSE:[/bold red] {result.get('root_cause', 'Unknown')}\n\n"
        f"[bold]Evidence:[/bold]\n" +
        "\n".join(f"  • {e}" for e in result.get("evidence", [])) +
        f"\n\n[bold]Context used:[/bold] {result.get('context_used', 'None')}\n\n"
        f"[bold green]Fix Suggestions:[/bold green]\n" +
        "\n".join(f"  {i+1}. {s}" for i, s in enumerate(result.get("fix_suggestions", []))),
        title=f"[bold red]⚠ RCA — {service_name}[/bold red]",
        border_style="red",
    ))


def _display_prediction(result: dict, service_name: str):
    will_fail  = result.get("will_fail", False)
    color      = "red" if will_fail else "green"
    icon       = "🔴" if will_fail else "🟢"
    console.print(Panel(
        f"{icon} [bold]Will fail:[/bold] {'YES' if will_fail else 'NO'}\n"
        f"[bold]Time to failure:[/bold] {result.get('estimated_time_to_failure', 'N/A')}\n"
        f"[bold]Trigger:[/bold] {result.get('failure_trigger', 'N/A')}\n"
        f"[bold]Headroom:[/bold] {result.get('current_headroom', 'N/A')}\n\n"
        f"[bold yellow]Context impact:[/bold yellow] {result.get('context_impact', 'None')}\n\n"
        f"[bold green]Recommendations:[/bold green]\n" +
        "\n".join(f"  {i+1}. {r}" for i, r in enumerate(result.get("recommendations", []))),
        title=f"[bold {color}]📈 Degradation Prediction — {service_name}[/bold {color}]",
        border_style=color,
    ))


def _display_load(result: dict):
    mult = result.get("expected_load_multiplier", 1.0)
    console.print(Panel(
        f"[bold]Expected load multiplier:[/bold] {mult}x normal\n"
        f"[bold]Peak window:[/bold] {result.get('peak_window', 'Unknown')}\n\n"
        f"[bold]Context events driving this forecast:[/bold]\n" +
        "\n".join(f"  • {e}" for e in result.get("context_events", [])) +
        f"\n\n[bold]At-risk services:[/bold]\n" +
        "\n".join(
            f"  {'❌' if not s['will_handle_load'] else '✅'} "
            f"{s['service']} — {s['reason']}"
            for s in result.get("at_risk_services", [])
        ) +
        f"\n\n[bold green]Scaling recommendations:[/bold green]\n" +
        "\n".join(f"  {i+1}. {r}" for i, r in enumerate(result.get("scaling_recommendations", []))),
        title="[bold magenta]📊 Load Prediction[/bold magenta]",
        border_style="magenta",
    ))


def _display_alert_grouping(result: dict):
    incidents = result.get("incidents", [])
    total_in  = result.get("total_alerts_in", 0)
    total_out = result.get("total_incidents_out", 0)
    noise_pct = result.get("noise_reduction_pct", 0)

    body = f"[bold]{total_in} alerts → {total_out} incident(s) ({noise_pct}% noise reduced)[/bold]\n\n"
    for inc in incidents:
        body += (
            f"[bold red]{inc['title']}[/bold red]\n"
            f"  Severity: {inc['severity']} | Root: {inc['root_service']}\n"
            f"  Affected: {', '.join(inc['affected_services'])}\n"
            f"  Reason: {inc['reason']}\n\n"
        )
    console.print(Panel(body, title="[bold yellow]🔔 Alert Noise Reduction[/bold yellow]", border_style="yellow"))


def _display_health_query(result: dict, question: str):
    unstable = result.get("unstable_services", [])
    console.print(Panel(
        f"[bold]Q:[/bold] {question}\n\n"
        f"[bold]A:[/bold] {result.get('answer', 'No data available.')}\n\n" +
        (
            "[bold red]Unstable services:[/bold red]\n" +
            "\n".join(f"  ❌ {s['service']}: {s['issue']}" for s in unstable) + "\n\n"
            if unstable else ""
        ) +
        f"[dim]{result.get('summary', '')}[/dim]",
        title="[bold cyan]💬 Health Query[/bold cyan]",
        border_style="cyan",
    ))


def _display_blast_radius(result: dict):
    chain = result.get("impact_chain", [])
    body  = (
        f"[bold red]Failing:[/bold red] {result.get('failing_service', '?')}\n"
        f"{result.get('failure_summary', '')}\n\n"
        f"[bold]Impact chain:[/bold]\n"
    )
    for svc in chain:
        prob  = svc.get("failure_probability_pct", 0)
        color = "red" if prob >= 70 else "yellow" if prob >= 40 else "green"
        body += (
            f"  [{color}]{svc['service']}[/{color}] — "
            f"{prob}% failure risk ({svc['impact_type']})\n"
            f"    {svc['business_impact']}\n"
        )

    safe = result.get("safe_services", [])
    if safe:
        # LLM sometimes returns list of strings, sometimes list of dicts
        safe_names = []
        for s in safe:
            if isinstance(s, dict):
                safe_names.append(s.get("service", str(s)))
            else:
                safe_names.append(str(s))
        body += f"\n[bold green]Safe services:[/bold green] {', '.join(safe_names)}\n"

    cbs = result.get("recommended_circuit_breakers", [])
    if cbs:
        body += "\n[bold]Circuit breakers to activate:[/bold]\n"
        # Same defensive handling for circuit breakers
        for c in cbs:
            body += f"  • {c if isinstance(c, str) else c.get('action', str(c))}\n"

    fix = result.get("fix_suggestions", [])
    if fix:
        body += "\n[bold green]Fix suggestions:[/bold green]\n"
        for i, f in enumerate(fix, 1):
            body += f"  {i}. {f if isinstance(f, str) else str(f)}\n"

    console.print(Panel(body, title="[bold red]💥 Blast Radius Estimation[/bold red]", border_style="red"))


# ── Context gap handler ───────────────────────────────────────────────────────

def _handle_context_gap(result: dict, system_prompt: str, user_prompt: str) -> dict:
    """
    If LLM confidence is low, ask the user for more context via CLI —
    but ONLY when a human is actually present to answer (see
    is_interactive() above). In every non-interactive case (headless
    mode, webhook-triggered runs, dashboard job threads, automatic
    collector-triggered runs), Prompt.ask() would block forever waiting
    for stdin input that will never arrive, silently hanging the entire
    analysis with no error and no way for the dashboard or webhook
    caller to know anything is wrong. Proceeding with the LLM's
    existing (lower-confidence) result is strictly better than hanging
    indefinitely — the result still gets returned, the confidence score
    still accurately reflects that the agent had a gap, and the caller
    can see that in the UI rather than waiting on a frozen job forever.
    """
    if not is_interactive():
        return result

    question = result.get("context_question", "Do you have any additional context?")

    console.print(Panel(
        f"[bold yellow]The agent needs more information to proceed confidently.[/bold yellow]\n\n"
        f"[bold]Agent asks:[/bold] {question}",
        title="[yellow]🤔 Agent Question[/yellow]",
        border_style="yellow",
    ))

    answer = Prompt.ask("[bold yellow]Your answer[/bold yellow]")

    if answer.strip():
        # Save to context store so it's available for all future reasoning
        add_user_context(answer, source="agent_question")
        console.print("[green]  ✓ Context saved. Re-running analysis...[/green]\n")

        # Rebuild user prompt with updated context (re-import to get fresh context)
        from db.database import get_context_as_text
        updated_ctx = get_context_as_text()
        updated_prompt = user_prompt + f"\n\nADDITIONAL CONTEXT FROM USER:\n{answer}"

        raw     = ask_llm(system_prompt, updated_prompt)
        result  = parse_json_response(raw)

    return result


# ── Mode runners ──────────────────────────────────────────────────────────────

def run_rca(service_name: str, signal: dict) -> dict:
    console.print(f"\n[bold]Running RCA for {service_name}...[/bold]")

    now         = datetime.now(timezone.utc)
    time_of_day = _get_time_of_day(now.hour)
    day_of_week = now.strftime("%A")

    # Intelligent semantic memory retrieval
    memory_text, used_memories, retrieval_method = build_memory_context(
        service_name=service_name,
        signal=signal,
        mode="rca",
        time_of_day=time_of_day,
    )
    console.print(f"  [dim]Memory: {retrieval_method}[/dim]")

    pkg, prompt = build_for_rca(service_name, signal)
    prompt = prompt + memory_text

    raw    = ask_llm(RCA_SYSTEM, prompt)
    result = parse_json_response(raw)

    if result.get("needs_more_context") and result.get("confidence", 100) < CONFIDENCE_THRESHOLD:
        result = _handle_context_gap(result, RCA_SYSTEM, prompt)

    _display_rca(result, service_name)
    display_used_memories(used_memories, service_name)
    _log.info(
        "RCA completed",
        service=service_name,
        confidence=result.get("confidence"),
        root_cause=result.get("root_cause", "")[:200],
        memory_method=retrieval_method,
    )

    result["mode"] = "rca"
    saved = insert_agent_output({
        "context_package_id":  pkg["id"],
        "mode":                "rca",
        "service_name":        service_name,
        "confidence":          result.get("confidence"),
        "needed_more_context": result.get("needs_more_context", False),
        "rca":                 result,
        "fix_suggestions":     result.get("fix_suggestions", []),
        "raw_llm_response":    raw,
    })

    # Store signal embedding for future semantic retrieval
    if saved and saved.get("id"):
        from agent.memory import build_signal_text, store_signal_embedding
        signal_text = build_signal_text(service_name, signal, "rca")
        store_signal_embedding(saved["id"], signal_text)

    extract_and_store_pattern(result, service_name, time_of_day, day_of_week)
    return result


def run_prediction(service_name: str, signal: dict) -> dict:
    console.print(f"\n[bold]Running degradation prediction for {service_name}...[/bold]")

    now         = datetime.now(timezone.utc)
    time_of_day = _get_time_of_day(now.hour)
    day_of_week = now.strftime("%A")

    # Intelligent semantic memory retrieval
    memory_text, used_memories, retrieval_method = build_memory_context(
        service_name=service_name,
        signal=signal,
        mode="predict_degradation",
        time_of_day=time_of_day,
    )
    console.print(f"  [dim]Memory: {retrieval_method}[/dim]")

    pkg, prompt = build_for_prediction(service_name, signal)
    prompt = prompt + memory_text

    raw    = ask_llm(PREDICT_SYSTEM, prompt)
    result = parse_json_response(raw)

    if result.get("needs_more_context") and result.get("confidence", 100) < CONFIDENCE_THRESHOLD:
        result = _handle_context_gap(result, PREDICT_SYSTEM, prompt)

    _display_prediction(result, service_name)
    display_used_memories(used_memories, service_name)

    result["mode"] = "predict_degradation"
    saved = insert_agent_output({
        "context_package_id":  pkg["id"],
        "mode":                "predict_degradation",
        "service_name":        service_name,
        "confidence":          result.get("confidence"),
        "needed_more_context": result.get("needs_more_context", False),
        "prediction":          result,
        "fix_suggestions":     result.get("recommendations", []),
        "raw_llm_response":    raw,
    })

    # Store signal embedding for future semantic retrieval
    if saved and saved.get("id"):
        from agent.memory import build_signal_text, store_signal_embedding
        signal_text = build_signal_text(service_name, signal, "predict_degradation")
        store_signal_embedding(saved["id"], signal_text)

    extract_and_store_pattern(result, service_name, time_of_day, day_of_week)
    return result


def run_load_prediction(service_name: str = None) -> dict:
    console.print("\n[bold]📊 Running load prediction...[/bold]")
    pkg, prompt = build_for_load_prediction(service_name)

    raw    = ask_llm(LOAD_SYSTEM, prompt)
    result = parse_json_response(raw)

    if result.get("needs_more_context") and result.get("confidence", 100) < CONFIDENCE_THRESHOLD:
        result = _handle_context_gap(result, LOAD_SYSTEM, prompt)

    _display_load(result)

    insert_agent_output({
        "context_package_id":  pkg["id"],
        "mode":                "load_prediction",
        "service_name":        service_name,
        "confidence":          result.get("confidence"),
        "needed_more_context": result.get("needs_more_context", False),
        "load_prediction":     result,
        "raw_llm_response":    raw,
    })
    return result


def run_alert_grouping() -> dict:
    console.print("\n[bold]🔔 Running alert noise reduction...[/bold]")
    alerts = get_ungrouped_alerts(window_minutes=10)

    if not alerts:
        console.print("[green]  No ungrouped alerts in the last 10 minutes.[/green]")
        return {}

    console.print(f"  Found {len(alerts)} ungrouped alerts.")
    pkg, prompt = build_for_alert_grouping(alerts)

    # Retrieve the short→full UUID map attached by context_builder
    id_map = pkg.pop("_id_map", {})

    raw    = ask_llm(ALERT_GROUPING_SYSTEM, prompt)
    result = parse_json_response(raw)

    if result.get("needs_more_context") and result.get("confidence", 100) < CONFIDENCE_THRESHOLD:
        result = _handle_context_gap(result, ALERT_GROUPING_SYSTEM, prompt)

    _display_alert_grouping(result)

    # Persist grouped incidents — resolve short IDs → full UUIDs before saving
    for inc in result.get("incidents", []):
        short_ids = inc.get("alert_ids_grouped", [])
        # Resolve every short ID to its full UUID; skip any that don't resolve
        full_ids = [id_map[sid] for sid in short_ids if sid in id_map]

        incident = insert_incident({
            "title":             inc["title"],
            "affected_services": inc["affected_services"],
            "raw_alert_count":   len(alerts),
            "suppressed_count":  inc.get("suppressed_count", 0),
            "status":            "open",
        })
        if full_ids:
            mark_alerts_grouped(full_ids, incident["id"])

    insert_agent_output({
        "context_package_id":  pkg["id"],
        "mode":                "alert_grouping",
        "confidence":          result.get("confidence"),
        "needed_more_context": result.get("needs_more_context", False),
        "alert_group":         result,
        "raw_llm_response":    raw,
    })
    return result


def run_health_query(question: str) -> dict:
    console.print(f"\n[bold]💬 Health query: {question}[/bold]")
    pkg, prompt = build_for_health_query(question)

    raw    = ask_llm(HEALTH_QUERY_SYSTEM, prompt)
    result = parse_json_response(raw)

    if result.get("needs_more_context") and result.get("confidence", 100) < CONFIDENCE_THRESHOLD:
        result = _handle_context_gap(result, HEALTH_QUERY_SYSTEM, prompt)

    _display_health_query(result, question)

    insert_agent_output({
        "context_package_id":  pkg["id"],
        "mode":                "health_query",
        "confidence":          result.get("confidence"),
        "needed_more_context": result.get("needs_more_context", False),
        "rca":                 result,   # reuse rca field for query result
        "raw_llm_response":    raw,
    })
    return result


def run_blast_radius(service_name: str, signal: dict = None) -> dict:
    console.print(f"\n[bold]Running blast radius for {service_name}...[/bold]")

    now         = datetime.now(timezone.utc)
    time_of_day = _get_time_of_day(now.hour)

    # Use empty signal if none provided
    _signal = signal or {}

    # Intelligent semantic memory retrieval
    memory_text, used_memories, retrieval_method = build_memory_context(
        service_name=service_name,
        signal=_signal,
        mode="blast_radius",
        time_of_day=time_of_day,
    )
    console.print(f"  [dim]Memory: {retrieval_method}[/dim]")

    pkg, prompt = build_for_blast_radius(service_name, signal)
    prompt = prompt + memory_text

    raw    = ask_llm(BLAST_RADIUS_SYSTEM, prompt)
    result = parse_json_response(raw)

    if result.get("needs_more_context") and result.get("confidence", 100) < CONFIDENCE_THRESHOLD:
        result = _handle_context_gap(result, BLAST_RADIUS_SYSTEM, prompt)

    _display_blast_radius(result)
    display_used_memories(used_memories, service_name)

    saved = insert_agent_output({
        "context_package_id":  pkg["id"],
        "mode":                "blast_radius",
        "service_name":        service_name,
        "confidence":          result.get("confidence"),
        "needed_more_context": result.get("needs_more_context", False),
        "blast_radius":        result,
        "fix_suggestions":     result.get("fix_suggestions", []),
        "raw_llm_response":    raw,
    })

    # Store signal embedding for future semantic retrieval
    if saved and saved.get("id"):
        signal_text = build_signal_text(service_name, _signal, "blast_radius")
        store_signal_embedding(saved["id"], signal_text)

    return result


# ── Main entry point called by collector ─────────────────────────────────────

def run_agent(trigger: str, service_name: str = None, signal: dict = None):
    """
    Called by the collector when an anomaly is detected.
    Runs RCA + prediction + blast radius + alert grouping.

    Rate limiting: 3s delay between LLM calls to stay within
    Groq free tier (8000 TPM). Each call uses ~1500-2000 tokens.
    4 calls × 2000 tokens = 8000 tokens — right at the limit.
    The delay lets the TPM window reset between calls.
    """
    console.rule(f"[bold red]AGENT TRIGGERED — {trigger.upper()}[/bold red]")

    if not signal:
        from db.database import get_latest_signal
        signal = get_latest_signal(service_name) or {}

    try:
        run_rca(service_name, signal)
    except Exception as e:
        console.print(f"[red]  RCA failed: {e}[/red]")

    console.print("[dim]  Waiting 4s before next LLM call (rate limit)...[/dim]")
    time.sleep(4)

    try:
        run_prediction(service_name, signal)
    except Exception as e:
        console.print(f"[red]  Prediction failed: {e}[/red]")

    console.print("[dim]  Waiting 4s before next LLM call (rate limit)...[/dim]")
    time.sleep(4)

    try:
        run_blast_radius(service_name, signal)
    except Exception as e:
        console.print(f"[red]  Blast radius failed: {e}[/red]")

    console.print("[dim]  Waiting 4s before next LLM call (rate limit)...[/dim]")
    time.sleep(4)

    try:
        run_alert_grouping()
    except Exception as e:
        console.print(f"[red]  Alert grouping failed: {e}[/red]")

    console.rule("[bold green]AGENT ANALYSIS COMPLETE[/bold green]")