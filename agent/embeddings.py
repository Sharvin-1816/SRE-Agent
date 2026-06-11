"""
agent/embeddings.py

Vector embeddings via LangChain HuggingFaceEmbeddings.
Model: all-MiniLM-L6-v2 (384-dim, ~80MB, cached after first use).
Public interface (embed, similarity, is_similar) is unchanged.
"""

import numpy as np
from rich.console import Console

console = Console()

_embeddings = None


def _get_embeddings():
    global _embeddings
    if _embeddings is None:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
            console.print("[dim]  Loading embedding model (first run only)...[/dim]")
            _embeddings = HuggingFaceEmbeddings(
                model_name="all-MiniLM-L6-v2",
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )
            console.print("[dim]  Embedding model ready.[/dim]")
        except ImportError:
            raise RuntimeError(
                "langchain-huggingface not installed.\n"
                "Run: pip install langchain-huggingface"
            )
    return _embeddings


def embed(text: str) -> list[float]:
    """Convert text to a 384-dimensional vector."""
    return _get_embeddings().embed_query(text)


def similarity(vec1: list[float], vec2: list[float]) -> float:
    """Cosine similarity — normalised vectors so dot product == cosine similarity."""
    a = np.array(vec1)
    b = np.array(vec2)
    return float(np.dot(a, b))


def is_similar(vec1: list[float], vec2: list[float], threshold: float = 0.85) -> bool:
    return similarity(vec1, vec2) >= threshold
