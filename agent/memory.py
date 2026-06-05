"""
agent/memory.py

Two-tier memory system for the SRE agent.

SHORT TERM  last 48h of agent decisions from agent_outputs table.
            Filtered by service name. Injected verbatim.

LONG TERM   extracted patterns from agent_memory_patterns table.
            Built up over time. Matched by service + time window.

Also handles:
  - extracting patterns from completed agent runs
  - tracking prediction outcomes
  - displaying which memories were used (transparency)
"""

import os
from datetime import datetime, timezone, timedelta
from rich.console import Console
from rich.table import Table

console = Console()

SHORT_TERM_LIMIT = int(os.getenv("MEMORY_SHORT_TERM_LIMIT", "5"))
LONG_TERM_LIMIT  = int(os.getenv("MEMORY_LONG_TERM_LIMIT",  "3"))


def _db():
    from db.database import db
    return db()


# ── Short term memory ─────────────────────────────────────────────────────────

def get_short_term_memory(service_name: str) -> list:
    since = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    result = (
        _db().table("agent_outputs")
        .select(
            "id, mode, service_name, confidence, needed_more_context, "
            "rca, prediction, blast_radius, alert_group, "
            "fix_suggestions, generated_at"
        )
        .eq("service_name", service_name)
        .gte("generated_at", since)
        .order("generated_at", desc=True)
        .limit(SHORT_TERM_LIMIT)
        .execute()
    )
    return result.data


def _extract_finding(output: dict) -> str:
    mode = output.get("mode", "")

    if mode == "rca":
        rca = output.get("rca") or {}
        if isinstance(rca, dict):
            return rca.get("root_cause", "Root cause analysis completed")

    elif mode == "predict_degradation":
        pred = output.get("prediction") or {}
        if isinstance(pred, dict):
            will_fail = pred.get("will_fail", False)
            ttf       = pred.get("estimated_time_to_failure", "unknown timeline")
            outcome   = pred.get("outcome", "")
            base      = f"Predicted {'WILL FAIL' if will_fail else 'will NOT fail'} — {ttf}"
            if outcome:
                base += f" [OUTCOME: {outcome}]"
            return base

    elif mode == "load_prediction":
        load = output.get("load_prediction") or {}
        if isinstance(load, dict):
            mult = load.get("expected_load_multiplier", "?")
            peak = load.get("peak_window", "unknown window")
            return f"Expected {mult}x load during {peak}"

    elif mode == "alert_grouping":
        grp = output.get("alert_group") or {}
        if isinstance(grp, dict):
            return (
                f"{grp.get('total_alerts_in','?')} alerts grouped into "
                f"{grp.get('total_incidents_out','?')} incidents "
                f"({grp.get('noise_reduction_pct','?')}% noise reduced)"
            )

    elif mode == "blast_radius":
        br = output.get("blast_radius") or {}
        if isinstance(br, dict):
            chain = br.get("impact_chain", [])
            if chain:
                top = chain[0]
                return (
                    f"Highest risk: {top.get('service','?')} at "
                    f"{top.get('failure_probability_pct','?')}% failure probability"
                )

    elif mode == "health_query":
        rca = output.get("rca") or {}
        if isinstance(rca, dict):
            return rca.get("answer", "Health query answered")

    return f"{mode} analysis completed"


def format_short_term_memory(memories: list) -> tuple:
    if not memories:
        return "No recent agent decisions for this service.", []

    used  = []
    lines = []
    for m in memories:
        ts      = m.get("generated_at", "")[:16]
        mode    = m.get("mode", "unknown").replace("_", " ").upper()
        conf    = m.get("confidence", 0)
        service = m.get("service_name", "unknown")
        finding = _extract_finding(m)

        lines.append(
            f"[{ts} UTC] {mode} on {service} (confidence: {conf}%)\n"
            f"  Finding: {finding}"
        )
        used.append({
            "type":       "short_term",
            "timestamp":  ts,
            "mode":       mode,
            "service":    service,
            "confidence": conf,
            "finding":    finding,
        })

    return "\n\n".join(lines), used


# ── Long term pattern memory ──────────────────────────────────────────────────

def get_long_term_patterns(service_name: str, time_of_day: str = None) -> list:
    q = (
        _db().table("agent_memory_patterns")
        .select("*")
        .eq("service_name", service_name)
        .order("occurrence_count", desc=True)
        .order("last_seen", desc=True)
        .limit(LONG_TERM_LIMIT)
    )
    if time_of_day:
        q = q.eq("time_of_day", time_of_day)

    result = q.execute()

    if not result.data and time_of_day:
        result = (
            _db().table("agent_memory_patterns")
            .select("*")
            .eq("service_name", service_name)
            .order("occurrence_count", desc=True)
            .limit(LONG_TERM_LIMIT)
            .execute()
        )

    return result.data


def format_long_term_memory(patterns: list) -> tuple:
    if not patterns:
        return "No long term patterns recorded for this service yet.", []

    used  = []
    lines = []
    for p in patterns:
        count      = p.get("occurrence_count", 1)
        cause      = p.get("root_cause", "unknown cause")
        resolution = p.get("resolution", "unknown resolution")
        outcome    = p.get("outcome", "unknown")
        time_ctx   = p.get("time_of_day", "")
        day_ctx    = p.get("day_of_week", "")
        last_seen  = p.get("last_seen", "")[:10]
        correct    = p.get("prediction_was_correct")
        summary    = p.get("raw_summary", "")

        time_str = ""
        if time_ctx and day_ctx:
            time_str = f" (typically during {day_ctx} {time_ctx})"
        elif time_ctx:
            time_str = f" (typically during {time_ctx})"

        correct_str = ""
        if correct is True:
            correct_str = " — prediction was CORRECT"
        elif correct is False:
            correct_str = " — prediction was INCORRECT"

        lines.append(
            f"Pattern seen {count}x (last: {last_seen}){time_str}:\n"
            f"  Root cause: {cause}\n"
            f"  Resolution: {resolution}\n"
            f"  Outcome: {outcome}{correct_str}\n"
            f"  Summary: {summary}"
        )
        used.append({
            "type":               "long_term_pattern",
            "service":            p.get("service_name"),
            "occurrences":        count,
            "root_cause":         cause,
            "resolution":         resolution,
            "last_seen":          last_seen,
            "prediction_correct": correct,
        })

    return "\n\n".join(lines), used


# ── Full memory context builder ───────────────────────────────────────────────

def build_memory_context(service_name: str, time_of_day: str = None) -> tuple:
    """
    Build the full memory context block for the LLM prompt.
    Returns (formatted_text, all_used_memories).
    all_used_memories is passed to display_used_memories() after the run.
    """
    short_raw              = get_short_term_memory(service_name)
    short_text, short_used = format_short_term_memory(short_raw)

    long_raw              = get_long_term_patterns(service_name, time_of_day)
    long_text, long_used  = format_long_term_memory(long_raw)

    all_used = short_used + long_used

    full_text = f"""
AGENT MEMORY -- {service_name.upper().replace('_', ' ')}
===============================================================
SHORT TERM MEMORY (last 48 hours):
{short_text}

LONG TERM PATTERNS (historical):
{long_text}
===============================================================
Use the above memory to:
1. Identify if this is a recurring pattern
2. Reference what worked as a resolution before
3. Adjust your confidence based on whether past predictions were correct
4. Note if this typically happens at this time of day
If memory shows this pattern has occurred before, state that explicitly
in your response and reference the past occurrence.
"""

    return full_text, all_used


# ── Pattern extraction ────────────────────────────────────────────────────────

def extract_and_store_pattern(
    agent_output: dict,
    service_name: str,
    time_of_day: str,
    day_of_week: str,
):
    """
    After an agent RCA or prediction completes, extract a structured
    pattern and store or update it in agent_memory_patterns.
    Uses one lightweight LLM call.
    """
    from agent.llm_adapter import ask_llm, parse_json_response

    mode = agent_output.get("mode", "")
    if mode not in ("rca", "predict_degradation"):
        return

    finding = _extract_finding(agent_output)

    system = (
        "You are extracting a structured memory pattern from an SRE agent analysis. "
        "Return ONLY valid JSON with these exact keys — no other text:\n"
        "{\n"
        '  "pattern_type": "short_label_no_spaces",\n'
        '  "root_cause": "one sentence",\n'
        '  "resolution": "one sentence — what fixed or would fix it",\n'
        '  "raw_summary": "2-3 sentences summarising the pattern"\n'
        "}"
    )

    user = (
        f"Service: {service_name}\n"
        f"Mode: {mode}\n"
        f"Time of day: {time_of_day}\n"
        f"Day of week: {day_of_week}\n"
        f"Finding: {finding}\n"
        f"Output excerpt: {str(agent_output)[:600]}\n\n"
        "Extract a reusable memory pattern."
    )

    try:
        raw     = ask_llm(system, user)
        pattern = parse_json_response(raw)
    except Exception as e:
        console.print(f"[dim]  Memory extraction skipped: {e}[/dim]")
        return

    existing = (
        _db().table("agent_memory_patterns")
        .select("id, occurrence_count")
        .eq("service_name", service_name)
        .eq("pattern_type", pattern.get("pattern_type", "unknown"))
        .execute()
    ).data

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        _db().table("agent_memory_patterns").update({
            "occurrence_count": existing[0]["occurrence_count"] + 1,
            "last_seen":        now,
            "root_cause":       pattern.get("root_cause"),
            "resolution":       pattern.get("resolution"),
            "raw_summary":      pattern.get("raw_summary"),
            "time_of_day":      time_of_day,
            "day_of_week":      day_of_week,
        }).eq("id", existing[0]["id"]).execute()
        console.print(
            f"[dim]  Memory updated: {pattern.get('pattern_type')} "
            f"(seen {existing[0]['occurrence_count'] + 1}x)[/dim]"
        )
    else:
        _db().table("agent_memory_patterns").insert({
            "service_name":     service_name,
            "pattern_type":     pattern.get("pattern_type", "unknown"),
            "time_of_day":      time_of_day,
            "day_of_week":      day_of_week,
            "root_cause":       pattern.get("root_cause"),
            "resolution":       pattern.get("resolution"),
            "outcome":          "unknown",
            "raw_summary":      pattern.get("raw_summary"),
            "occurrence_count": 1,
            "first_seen":       now,
            "last_seen":        now,
        }).execute()
        console.print(
            f"[dim]  New memory pattern stored: {pattern.get('pattern_type')}[/dim]"
        )


# ── Display used memories ─────────────────────────────────────────────────────

def display_used_memories(used_memories: list, service_name: str):
    """
    Display exactly which past memories the agent drew from.
    Shown after every agent analysis — full transparency on reasoning trail.
    No emojis. Plain text table.
    """
    if not used_memories:
        console.print(
            f"[dim]  Memory: no past decisions found for {service_name}[/dim]"
        )
        return

    table = Table(
        title=f"Memory used for reasoning — {service_name}",
        show_lines=True,
        style="dim",
    )
    table.add_column("Memory type",   width=18)
    table.add_column("When",          width=12)
    table.add_column("Mode / Pattern", width=24)
    table.add_column("Key finding",   style="white")

    for m in used_memories:
        mem_type = m.get("type", "")

        if mem_type == "short_term":
            table.add_row(
                "short term",
                m.get("timestamp", "")[:10],
                m.get("mode", "").title(),
                m.get("finding", ""),
            )

        elif mem_type == "long_term_pattern":
            correct = m.get("prediction_correct")
            suffix  = ""
            if correct is True:
                suffix = " [prediction correct]"
            elif correct is False:
                suffix = " [prediction incorrect]"

            table.add_row(
                f"long term ({m.get('occurrences',1)}x seen)",
                m.get("last_seen", ""),
                m.get("root_cause", "")[:24],
                m.get("resolution", "") + suffix,
            )

    console.print(table)
