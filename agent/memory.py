"""
agent/memory.py

Two-tier memory system with intelligent semantic retrieval.

SHORT TERM  — semantic search over agent_outputs using pgvector.
              Embeds the current anomaly signal and finds the most
              semantically similar past decisions — not just the most
              recent ones. Filtered by relevant modes for the current task.

LONG TERM   — semantic search over agent_memory_patterns using pgvector.
              Finds structurally similar past incidents by root cause
              similarity, not just occurrence count.

The shift from recency/frequency → semantic similarity means the agent
gets the most relevant past experience for exactly what is happening now,
regardless of when it happened or how often.

Also handles:
  - embedding current signals for storage (called by agent_loop)
  - extracting and storing patterns after each run
  - displaying which memories were used (full transparency)
"""

import os
from datetime import datetime, timezone, timedelta
from rich.console import Console
from rich.table import Table

console = Console()

SHORT_TERM_LIMIT = int(os.getenv("MEMORY_SHORT_TERM_LIMIT", "5"))
LONG_TERM_LIMIT  = int(os.getenv("MEMORY_LONG_TERM_LIMIT",  "3"))
SIMILARITY_THRESHOLD = float(os.getenv("MEMORY_SIMILARITY_THRESHOLD", "0.70"))

# Mode relevance map — which past modes are useful for each current mode
MODE_RELEVANCE = {
    "rca": [
        "rca",                # past RCAs for same service — directly relevant
        "predict_degradation",# past predictions — tells us if this pattern recurs
    ],
    "predict_degradation": [
        "predict_degradation", # past predictions — did they come true?
        "rca",                 # past RCAs — what caused it last time?
    ],
    "blast_radius": [
        "blast_radius",        # past blast radius — how did cascade play out?
        "rca",                 # what was root cause that caused cascade?
    ],
    "load_prediction": [
        "load_prediction",     # past load predictions — were they accurate?
    ],
    "alert_grouping": [
        "alert_grouping",      # past groupings — what was grouped before?
    ],
    "health_query": [
        "rca",
        "predict_degradation",
        "health_query",
    ],
}


def _db():
    from db.database import db
    return db()


# ── Signal text builder ───────────────────────────────────────────────────────

def build_signal_text(service_name: str, signal: dict, mode: str) -> str:
    """
    Build a plain text representation of the current anomaly signal.
    This is what gets embedded and stored with each agent output,
    and what gets embedded at retrieval time for similarity search.
    """
    parts = [f"service: {service_name}", f"mode: {mode}"]

    summary = signal.get("human_summary", "")
    if summary:
        parts.append(f"summary: {summary[:300]}")

    severity = signal.get("severity", "")
    if severity:
        parts.append(f"severity: {severity}")

    snapshot = signal.get("metrics_snapshot", {})
    for metric, data in snapshot.items():
        if isinstance(data, dict):
            val = data.get("current_value", "")
            z   = data.get("z_score", "")
            if val or z:
                parts.append(f"{metric}: value={val} z_score={z}")

    trend = signal.get("trend_summary", "")
    if trend:
        parts.append(f"trend: {trend[:200]}")

    hints = signal.get("hypothesis_hints", [])
    if hints:
        parts.append(f"hints: {'; '.join(hints[:3])}")

    return " | ".join(parts)


# ── Semantic short term retrieval ─────────────────────────────────────────────

def get_relevant_short_term_memory(
    service_name: str,
    signal_text: str,
    mode: str,
) -> list:
    """
    Semantic search over agent_outputs using pgvector.
    Finds past decisions most similar to the current anomaly signal.

    Falls back to recency-based retrieval if:
      - pgvector RPC is unavailable
      - No embeddings stored yet (new system)
      - signal_text is empty
    """
    if not signal_text:
        return _recency_fallback(service_name)

    try:
        from agent.embeddings import embed
        embedding = embed(signal_text)

        # Get relevant modes for this agent run
        relevant_modes = MODE_RELEVANCE.get(mode, [mode])

        all_results = []
        seen_ids    = set()

        # Search per relevant mode to get best matches across modes
        for search_mode in relevant_modes:
            try:
                result = _db().rpc(
                    "match_agent_outputs",
                    {
                        "p_service":   service_name,
                        "p_embedding": embedding,
                        "p_mode":      search_mode,
                        "p_threshold": SIMILARITY_THRESHOLD,
                        "p_limit":     SHORT_TERM_LIMIT,
                    }
                ).execute()

                for row in (result.data or []):
                    if row["id"] not in seen_ids:
                        seen_ids.add(row["id"])
                        row["_similarity"] = row.get("similarity", 0)
                        all_results.append(row)

            except Exception:
                continue

        if all_results:
            # Sort by similarity descending, take top N
            all_results.sort(key=lambda r: r.get("_similarity", 0), reverse=True)
            return all_results[:SHORT_TERM_LIMIT]

        # No vector results — fall back to recency
        return _recency_fallback(service_name)

    except Exception as e:
        console.print(f"[dim]  Semantic memory search failed, using recency: {e}[/dim]")
        return _recency_fallback(service_name)


def _recency_fallback(service_name: str) -> list:
    """Plain recency-based retrieval as fallback when vectors unavailable."""
    since = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    try:
        result = (
            _db().table("agent_outputs")
            .select(
                "id, mode, service_name, confidence, needed_more_context, "
                "rca, prediction, blast_radius, alert_group, "
                "fix_suggestions, generated_at, signal_text"
            )
            .eq("service_name", service_name)
            .gte("generated_at", since)
            .order("generated_at", desc=True)
            .limit(SHORT_TERM_LIMIT)
            .execute()
        )
        return result.data
    except Exception:
        return []


# ── Semantic long term retrieval ──────────────────────────────────────────────

def get_relevant_long_term_patterns(
    service_name: str,
    signal_text: str,
    time_of_day: str = None,
) -> list:
    """
    Semantic search over agent_memory_patterns using pgvector.
    Finds structurally similar past incidents by root cause similarity.

    Falls back to occurrence-count ordering if vectors unavailable.
    """
    if not signal_text:
        return _pattern_fallback(service_name, time_of_day)

    try:
        from agent.embeddings import embed
        embedding = embed(signal_text)

        result = _db().rpc(
            "find_similar_memory_pattern",
            {
                "p_service":   service_name,
                "p_embedding": embedding,
                "p_threshold": SIMILARITY_THRESHOLD,
                "p_limit":     LONG_TERM_LIMIT,
            }
        ).execute()

        if result.data:
            return result.data

        return _pattern_fallback(service_name, time_of_day)

    except Exception as e:
        console.print(f"[dim]  Semantic pattern search failed, using fallback: {e}[/dim]")
        return _pattern_fallback(service_name, time_of_day)


def _pattern_fallback(service_name: str, time_of_day: str = None) -> list:
    """Occurrence-count based retrieval as fallback."""
    try:
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
    except Exception:
        return []


# ── Formatting ────────────────────────────────────────────────────────────────

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


def format_short_term_memory(memories: list, semantic: bool = False) -> tuple:
    if not memories:
        return "No relevant past decisions found for this service.", []

    used  = []
    lines = []
    for m in memories:
        ts       = m.get("generated_at", "")[:16]
        mode     = m.get("mode", "unknown").replace("_", " ").upper()
        conf     = m.get("confidence", 0)
        service  = m.get("service_name", "unknown")
        finding  = _extract_finding(m)
        sim      = m.get("_similarity")
        sim_str  = f" [similarity: {sim:.2f}]" if sim else ""

        lines.append(
            f"[{ts} UTC] {mode} on {service} (confidence: {conf}%){sim_str}\n"
            f"  Finding: {finding}"
        )
        used.append({
            "type":       "short_term",
            "timestamp":  ts,
            "mode":       mode,
            "service":    service,
            "confidence": conf,
            "finding":    finding,
            "similarity": sim,
            "semantic":   semantic,
        })

    retrieval_note = "semantic search" if semantic else "recency"
    return "\n\n".join(lines), used, retrieval_note


def format_long_term_memory(patterns: list, semantic: bool = False) -> tuple:
    if not patterns:
        return "No relevant long term patterns found for this service.", []

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
        sim        = p.get("similarity")
        sim_str    = f" [similarity: {sim:.2f}]" if sim else ""

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
            f"Pattern seen {count}x (last: {last_seen}){time_str}{sim_str}:\n"
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
            "similarity":         sim,
            "semantic":           semantic,
        })

    return "\n\n".join(lines), used


# ── Full memory context builder ───────────────────────────────────────────────

def build_memory_context(
    service_name: str,
    signal: dict,
    mode: str,
    time_of_day: str = None,
) -> tuple:
    """
    Build the full memory context block for the LLM prompt.

    Uses semantic search (pgvector) to find the most relevant past
    decisions and patterns for the current anomaly signal.

    Returns (formatted_text, all_used_memories, retrieval_method).
    """
    # Build signal text for embedding
    signal_text = build_signal_text(service_name, signal, mode)

    # Short term — semantic search
    short_raw = get_relevant_short_term_memory(service_name, signal_text, mode)
    semantic_short = any(r.get("_similarity") for r in short_raw)
    short_result = format_short_term_memory(short_raw, semantic=semantic_short)

    if len(short_result) == 3:
        short_text, short_used, short_method = short_result
    else:
        short_text, short_used = short_result
        short_method = "recency"

    # Long term — semantic search
    long_raw  = get_relevant_long_term_patterns(service_name, signal_text, time_of_day)
    semantic_long = any(r.get("similarity") for r in long_raw)
    long_text, long_used = format_long_term_memory(long_raw, semantic=semantic_long)

    all_used = short_used + long_used

    retrieval_method = "semantic" if (semantic_short or semantic_long) else "recency"

    full_text = f"""
AGENT MEMORY -- {service_name.upper().replace('_', ' ')}
Retrieval method: {retrieval_method} (most relevant to current signal)
Relevant modes: {', '.join(MODE_RELEVANCE.get(mode, [mode]))}
===============================================================
SHORT TERM MEMORY (semantically relevant past decisions):
{short_text}

LONG TERM PATTERNS (semantically similar historical incidents):
{long_text}
===============================================================
Use the above memory to:
1. Identify if this is a recurring pattern — state it explicitly if so
2. Reference what resolution worked before
3. Adjust confidence based on whether past predictions were correct
4. Note if this typically happens at this time of day
If memory shows similarity > 0.85 to a past incident, treat it as
the same pattern and reference it directly in your reasoning.
"""

    return full_text, all_used, retrieval_method


# ── Store signal embedding after agent run ────────────────────────────────────

def store_signal_embedding(output_id: str, signal_text: str):
    """
    Store the embedding of the anomaly signal alongside the agent output.
    Called by agent_loop after each run so future searches can find it.
    """
    if not signal_text or not output_id:
        return
    try:
        from agent.embeddings import embed
        embedding = embed(signal_text)
        _db().table("agent_outputs").update({
            "signal_embedding": embedding,
            "signal_text":      signal_text,
        }).eq("id", output_id).execute()
    except Exception as e:
        console.print(f"[dim]  Signal embedding storage failed: {e}[/dim]")


# ── Pattern extraction ────────────────────────────────────────────────────────

def extract_and_store_pattern(
    agent_output: dict,
    service_name: str,
    time_of_day: str,
    day_of_week: str,
):
    """
    After an agent RCA or prediction completes, extract a structured
    pattern and store or update it using vector similarity deduplication.
    """
    from agent.llm_adapter import ask_llm, parse_json_response
    from agent.embeddings  import embed
    from db.database       import upsert_memory_pattern_with_vector

    mode = agent_output.get("mode", "")
    if mode not in ("rca", "predict_degradation"):
        return

    finding = _extract_finding(agent_output)

    system = (
        "You are extracting a structured memory pattern from an SRE agent analysis. "
        "Return ONLY valid JSON with these exact keys — no other text:\n"
        "{\n"
        '  "pattern_type": "short_label_no_spaces",\n'
        '  "root_cause": "one sentence — be specific about the technical cause",\n'
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

    root_cause = pattern.get("root_cause", "")
    if not root_cause:
        return

    try:
        embedding = embed(root_cause)
    except Exception as e:
        console.print(f"[dim]  Embedding failed, using exact match: {e}[/dim]")
        _simple_upsert(pattern, service_name, time_of_day, day_of_week)
        return

    record, was_new = upsert_memory_pattern_with_vector(
        pattern={
            "service_name": service_name,
            "pattern_type": pattern.get("pattern_type", "unknown"),
            "time_of_day":  time_of_day,
            "day_of_week":  day_of_week,
            "root_cause":   root_cause,
            "resolution":   pattern.get("resolution"),
            "outcome":      "unknown",
            "raw_summary":  pattern.get("raw_summary"),
        },
        embedding=embedding,
        threshold=0.85,
    )

    if was_new:
        console.print(
            f"[dim]  New memory pattern stored: "
            f"{pattern.get('pattern_type')} for {service_name}[/dim]"
        )
    else:
        console.print(
            f"[dim]  Existing memory updated (vector match): "
            f"{record.get('pattern_type')} "
            f"(seen {record.get('occurrence_count', '?')}x)[/dim]"
        )


def _simple_upsert(pattern: dict, service_name: str, time_of_day: str, day_of_week: str):
    now      = datetime.now(timezone.utc).isoformat()
    existing = (
        _db().table("agent_memory_patterns")
        .select("id, occurrence_count")
        .eq("service_name", service_name)
        .eq("pattern_type", pattern.get("pattern_type", "unknown"))
        .execute()
    ).data

    if existing:
        _db().table("agent_memory_patterns").update({
            "occurrence_count": existing[0]["occurrence_count"] + 1,
            "last_seen":        now,
            "root_cause":       pattern.get("root_cause"),
            "resolution":       pattern.get("resolution"),
            "raw_summary":      pattern.get("raw_summary"),
        }).eq("id", existing[0]["id"]).execute()
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


# ── Display used memories ─────────────────────────────────────────────────────

def display_used_memories(used_memories: list, service_name: str):
    """
    Display exactly which past memories the agent drew from.
    Shows similarity scores so the user can see how relevant each memory is.
    """
    if not used_memories:
        console.print(f"[dim]  Memory: no relevant past decisions found for {service_name}[/dim]")
        return

    semantic_count = sum(1 for m in used_memories if m.get("semantic"))
    method = "semantic search" if semantic_count > 0 else "recency fallback"

    table = Table(
        title=f"Memory used for reasoning — {service_name} ({method})",
        show_lines=True,
        style="dim",
    )
    table.add_column("Type",          width=16)
    table.add_column("When",          width=12)
    table.add_column("Mode / Pattern", width=22)
    table.add_column("Similarity",    width=10, justify="right")
    table.add_column("Key finding",   style="white")

    for m in used_memories:
        mem_type = m.get("type", "")
        sim      = m.get("similarity")
        sim_str  = f"{sim:.2f}" if sim else "—"

        if mem_type == "short_term":
            table.add_row(
                "short term",
                m.get("timestamp", "")[:10],
                m.get("mode", "").title(),
                sim_str,
                m.get("finding", ""),
            )
        elif mem_type == "long_term_pattern":
            correct = m.get("prediction_correct")
            suffix  = ""
            if correct is True:
                suffix = " [correct]"
            elif correct is False:
                suffix = " [incorrect]"

            table.add_row(
                f"long term ({m.get('occurrences',1)}x)",
                m.get("last_seen", ""),
                m.get("root_cause", "")[:22],
                sim_str,
                m.get("resolution", "") + suffix,
            )

    console.print(table)