"""
agent/llm_adapter.py

Single swap layer for all LLM providers.
Every agent mode calls ask_llm() — nothing else touches the LLM directly.

To switch providers: change LLM_PROVIDER in .env
    LLM_PROVIDER=ollama   → uses local Ollama
    LLM_PROVIDER=groq     → uses Groq cloud API (free tier)

Adding a new provider in future:
    1. Add a new _ask_<provider>() function
    2. Add it to the dispatch dict in ask_llm()
    That's it — no other file changes.
"""

import os
import json
import httpx
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

PROVIDER     = os.getenv("LLM_PROVIDER",  "ollama")
OLLAMA_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("LLM_MODEL",      "llama3.1")
GROQ_KEY     = os.getenv("GROQ_API_KEY",   "")
GROQ_MODEL   = os.getenv("GROQ_MODEL",     "llama-3.1-70b-versatile")


# ── Ollama ────────────────────────────────────────────────────────────────────

def _ask_ollama(system_prompt: str, user_prompt: str) -> str:
    """Call local Ollama instance."""
    payload = {
        "model":  OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system",  "content": system_prompt},
            {"role": "user",    "content": user_prompt},
        ],
    }
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=120.0,   # local LLM can be slow
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except httpx.ConnectError:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_URL}. "
            "Is Ollama running? Try: ollama serve"
        )
    except Exception as e:
        raise RuntimeError(f"Ollama error: {e}")


# ── Groq ──────────────────────────────────────────────────────────────────────

def _ask_groq(system_prompt: str, user_prompt: str) -> str:
    """Call Groq cloud API."""
    if not GROQ_KEY:
        raise RuntimeError("GROQ_API_KEY not set in .env")

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.2,    # low temp for consistent structured output
        "max_tokens":  2048,
    }
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Groq API error {e.response.status_code}: {e.response.text}")
    except Exception as e:
        raise RuntimeError(f"Groq error: {e}")


# ── Public interface ──────────────────────────────────────────────────────────

def ask_llm(system_prompt: str, user_prompt: str) -> str:
    """
    Single entry point for all LLM calls.
    Tries primary provider, falls back to Groq if Ollama fails.
    """
    providers = {
        "ollama": _ask_ollama,
        "groq":   _ask_groq,
    }

    primary = PROVIDER.lower()
    fallback = "groq" if primary == "ollama" else "ollama"

    # Try primary
    try:
        console.print(f"  [dim]LLM → {primary} ({OLLAMA_MODEL if primary == 'ollama' else GROQ_MODEL})[/dim]")
        return providers[primary](system_prompt, user_prompt)
    except RuntimeError as e:
        console.print(f"  [yellow]Primary LLM ({primary}) failed: {e}[/yellow]")

    # Try fallback
    try:
        console.print(f"  [dim]LLM fallback → {fallback}[/dim]")
        return providers[fallback](system_prompt, user_prompt)
    except RuntimeError as e:
        raise RuntimeError(
            f"Both LLM providers failed.\n"
            f"Primary ({primary}): unavailable\n"
            f"Fallback ({fallback}): {e}\n\n"
            f"Check: is Ollama running? Is GROQ_API_KEY set?"
        )


def parse_json_response(raw: str) -> dict:
    """
    Safely parse LLM response as JSON.
    Handles cases where the LLM wraps output in markdown code fences.
    """
    text = raw.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Last resort: try to find JSON object in the response
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