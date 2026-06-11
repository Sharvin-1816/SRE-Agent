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
        max_tokens=2048,
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
