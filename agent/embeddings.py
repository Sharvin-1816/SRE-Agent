"""
agent/embeddings.py

Generates vector embeddings using HuggingFaceEmbeddings from langchain-huggingface.
Model: all-MiniLM-L6-v2 (384 dimensions, runs locally, no API calls)
"""

import os

# MUST be set before transformers/langchain_huggingface is imported anywhere
# in the process — both libraries decide whether to attempt loading
# TensorFlow at import time, deep inside their own module graph, not at
# any point code calling them controls. Setting this here (rather than
# relying on .env + load_dotenv() running first in whichever entry point
# happens to start the process) guarantees it's set regardless of import
# order across main.py / start.py / dashboard_api.py / etc.
#
# Why this is needed at all: on this project's dev machine (Windows,
# Python 3.13), TensorFlow itself is broken — DLL load failure unrelated
# to this codebase (matches transformers GitHub issue #40292, same
# failure on the same Python/OS combination). transformers' import chain
# attempts to import TensorFlow as a side effect of importing
# sentence_transformers, even though this project only ever uses the
# PyTorch backend. USE_TF=0 stops that attempt at the source. The
# newer TRANSFORMERS_NO_TF flag is deliberately NOT used here because
# it's confirmed unreliable in current transformers versions on Python
# 3.13 (same GitHub issue) — USE_TF is the older flag and was the one
# actually confirmed working in this project (2026-06-18 manual test).
#
# Without this, every embed() call silently fails through both the
# HuggingFaceEmbeddings path and the SentenceTransformer fallback path
# (both fail for the same underlying TensorFlow reason), landing on the
# "No embedding library found" error even though the real libraries ARE
# installed — semantic memory retrieval then always uses the recency
# fallback instead, which works but loses the actual similarity-based
# retrieval this file exists to provide.
os.environ.setdefault("USE_TF", "0")

from rich.console import Console
import numpy as np

console = Console()

_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
            console.print("[dim]  Loading embedding model (first run only)...[/dim]")
            _model = HuggingFaceEmbeddings(
                model_name="all-MiniLM-L6-v2",
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )
            console.print("[dim]  Embedding model ready.[/dim]")
        except ImportError:
            # Fall back to sentence-transformers directly
            try:
                from sentence_transformers import SentenceTransformer
                console.print("[dim]  Loading embedding model (first run only)...[/dim]")
                _model = SentenceTransformer("all-MiniLM-L6-v2")
                _model._is_sentence_transformer = True
                console.print("[dim]  Embedding model ready.[/dim]")
            except ImportError:
                raise RuntimeError(
                    "No embedding library found.\n"
                    "Run: pip install langchain-huggingface"
                )
    return _model


def embed(text: str) -> list[float]:
    model = _get_model()
    # Handle both HuggingFaceEmbeddings and SentenceTransformer
    if hasattr(model, "_is_sentence_transformer"):
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()
    else:
        return model.embed_query(text)


def similarity(vec1: list[float], vec2: list[float]) -> float:
    a = np.array(vec1)
    b = np.array(vec2)
    return float(np.dot(a, b))


def is_similar(vec1: list[float], vec2: list[float], threshold: float = 0.85) -> bool:
    return similarity(vec1, vec2) >= threshold