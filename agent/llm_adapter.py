"""
agent/llm_adapter.py

LLM provider layer — powered by LangChain.
Every agent mode calls ask_llm() — nothing else touches the LLM directly.

To switch providers: change LLM_PROVIDER in .env
    LLM_PROVIDER=ollama   → uses local Ollama
    LLM_PROVIDER=groq     → uses Groq cloud API (free tier)
"""

import os
import json
from typing import Optional, List
from dotenv import load_dotenv
from rich.console import Console

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

load_dotenv()
console = Console()

PROVIDER     = os.getenv("LLM_PROVIDER",    "ollama")
OLLAMA_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("LLM_MODEL",       "llama3.1")
GROQ_KEY     = os.getenv("GROQ_API_KEY",    "")
GROQ_MODEL   = os.getenv("GROQ_MODEL",      "llama-3.1-70b-versatile")


def _build_groq() -> ChatGroq:
    if not GROQ_KEY:
        raise RuntimeError("GROQ_API_KEY not set in .env")
    return ChatGroq(
        model=GROQ_MODEL,
        api_key=GROQ_KEY,
        temperature=0.2,
        # 2048 was occasionally too tight for responses with several list
        # fields (fix_suggestions, recommendations, impact_chain, etc.) —
        # a verbose completion could get cut off mid-JSON before the model
        # reached the closing brace, which then fails to parse downstream.
        # This alone doesn't guarantee no truncation ever happens again
        # (hence the retry in ask_llm_json below), but it removes the most
        # common, easily-avoidable cause of it.
        max_tokens=3072,
    )


def _build_ollama() -> ChatOllama:
    return ChatOllama(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_URL,
        temperature=0.2,
    )


def _invoke_with_fallback(messages: list) -> str:
    """Shared core — invoke with primary provider, fall back on failure."""
    primary_name  = PROVIDER.lower()
    fallback_name = "groq" if primary_name == "ollama" else "ollama"
    builders      = {"groq": _build_groq, "ollama": _build_ollama}
    model_label   = OLLAMA_MODEL if primary_name == "ollama" else GROQ_MODEL

    try:
        console.print(f"  [dim]LLM → {primary_name} ({model_label})[/dim]")
        return builders[primary_name]().invoke(messages).content
    except Exception as e:
        console.print(f"  [yellow]Primary LLM ({primary_name}) failed: {e}[/yellow]")

    try:
        console.print(f"  [dim]LLM fallback → {fallback_name}[/dim]")
        return builders[fallback_name]().invoke(messages).content
    except Exception as e:
        raise RuntimeError(
            f"Both LLM providers failed.\n"
            f"Primary ({primary_name}): unavailable\n"
            f"Fallback ({fallback_name}): {e}\n\n"
            f"Check: is Ollama running? Is GROQ_API_KEY set?"
        )


# ── Public interface ──────────────────────────────────────────────────────────

def ask_llm(system_prompt: str, user_prompt: str) -> str:
    """Single entry point for raw string LLM calls."""
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    return _invoke_with_fallback(messages)


def ask_llm_from_template(template, context: str) -> str:
    """Invoke the LLM using a ChatPromptTemplate with a {context} variable."""
    messages = template.format_messages(context=context)
    return _invoke_with_fallback(messages)


def ask_llm_json(system_prompt: str, user_prompt: str, max_retries: int = 1) -> tuple[dict, str]:
    """
    ask_llm() + parse_json_response() combined, with a retry on parse
    failure specifically (not on network/auth errors, which still raise
    immediately — retrying those just wastes time on something that will
    fail the same way again).

    Why this exists: Groq occasionally returns a response that gets cut
    off before the JSON closes (hit a max_tokens boundary, or a transient
    provider hiccup) — see the load_prediction failure logged 2026-06-17
    21:02 ("Unterminated string..."). parse_json_response()'s fallback
    chain (strip fences, extract largest {...}) can only ever work on a
    SINGLE response — none of those steps can recover a response that was
    truncated mid-stream, because the valid JSON simply isn't in the text
    at all. The only real fix for that specific failure mode is asking
    again. One retry catches the common transient case without masking a
    persistently broken prompt — if it fails twice in a row, that's a
    different problem (bad prompt, bad schema, provider outage) and we
    surface the real error rather than retrying forever.

    Returns (parsed_dict, raw_string_that_was_successfully_parsed) — every
    one of agent_loop.py's six mode runners stores the raw string into
    insert_agent_output's raw_llm_response field for audit/replay, so this
    can't just return the dict alone without losing that. If a retry
    happened, the raw string returned is the SECOND (successful) response,
    not the first truncated one — the audit trail should reflect what
    actually produced the result, not the attempt that failed.
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 2):  # e.g. max_retries=1 -> tries 1 and 2
        raw = ask_llm(system_prompt, user_prompt)
        try:
            parsed = parse_json_response(raw)
            return parsed, raw
        except ValueError as e:
            last_error = e
            if attempt <= max_retries:
                console.print(
                    f"  [yellow]LLM returned malformed JSON "
                    f"(attempt {attempt}/{max_retries + 1}) — retrying...[/yellow]"
                )
            continue

    # All attempts exhausted — raise the most recent parse error so the
    # caller's existing error handling (e.g. dashboard_api.py's job error
    # capture) sees the same exception shape it already handles today.
    raise last_error


# ── Pydantic output schemas ───────────────────────────────────────────────────

class RCAOutput(BaseModel):
    root_cause: str              = Field(description="Primary root cause of the failure")
    contributing_factors: List[str] = Field(description="Secondary factors")
    immediate_actions: List[str] = Field(description="Steps to take right now")
    confidence: int              = Field(description="Confidence score 0-100")
    needs_more_context: bool     = Field(description="True if more info is needed")
    clarifying_question: Optional[str] = Field(default=None)

class PredictionOutput(BaseModel):
    time_to_failure_mins: Optional[int] = Field(default=None)
    predicted_failure_mode: str
    risk_level: str
    recommended_actions: List[str]
    confidence: int
    needs_more_context: bool
    clarifying_question: Optional[str] = Field(default=None)

class BlastRadiusOutput(BaseModel):
    directly_affected: List[str]
    indirectly_affected: List[str]
    severity_by_service: dict
    estimated_user_impact_pct: Optional[float] = Field(default=None)
    recommended_actions: List[str]
    confidence: int
    needs_more_context: bool
    clarifying_question: Optional[str] = Field(default=None)


def ask_llm_structured(system_prompt: str, user_prompt: str, schema: type[BaseModel]) -> BaseModel:
    """
    LLM call with structured Pydantic output via LangChain JsonOutputParser.
    Returns a validated instance of `schema`. Falls back gracefully to
    parse_json_response + manual construction if the parser fails.
    """
    parser = JsonOutputParser(pydantic_object=schema)
    format_instructions = parser.get_format_instructions()

    augmented_system = f"{system_prompt}\n\n{format_instructions}"

    raw = ask_llm(augmented_system, user_prompt)
    try:
        return parser.parse(raw)
    except Exception:
        data = parse_json_response(raw)
        return schema(**data)


def parse_json_response(raw: str) -> dict:
    """
    Safely parse LLM response as JSON.
    Handles cases where the LLM wraps output in markdown code fences.
    """
    text = raw.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Last resort: find JSON object in the response
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end])
            except Exception:
                pass
        raise ValueError(
            f"LLM did not return valid JSON.\n"
            f"Parse error: {e}\n"
            f"Raw response (first 500 chars):\n{raw[:500]}"
        )