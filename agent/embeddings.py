"""
agent/embeddings.py

Generates vector embeddings for memory pattern deduplication.
Uses sentence-transformers running locally — no API calls, no cost.

Model: all-MiniLM-L6-v2
  - 80MB download on first use (cached after that)
  - 384 dimensions
  - Fast inference (~5ms per sentence on CPU)
  - Good semantic similarity for technical SRE text

Usage:
  from agent.embeddings import embed, similarity
  vec = embed("DB connection pool exhaustion after deployment")
  sim = similarity(vec1, vec2)   # 0.0 to 1.0
"""

import numpy as np
from rich.console import Console

console = Console()

_model = None


def _get_model():
    """Lazy load the model — only downloads on first use."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            console.print("[dim]  Loading embedding model (first run only)...[/dim]")
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            console.print("[dim]  Embedding model ready.[/dim]")
        except ImportError:
            raise RuntimeError(
                "sentence-transformers not installed.\n"
                "Run: pip install sentence-transformers"
            )
    return _model


def embed(text: str) -> list[float]:
    """
    Convert text to a 384-dimensional vector.
    Returns a plain Python list for Supabase storage.
    """
    model  = _get_model()
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def similarity(vec1: list[float], vec2: list[float]) -> float:
    """
    Cosine similarity between two vectors.
    Returns a float between 0.0 (unrelated) and 1.0 (identical).
    Since vectors are normalised, dot product = cosine similarity.
    """
    a = np.array(vec1)
    b = np.array(vec2)
    return float(np.dot(a, b))


def is_similar(vec1: list[float], vec2: list[float], threshold: float = 0.85) -> bool:
    """Returns True if two vectors are semantically similar."""
    return similarity(vec1, vec2) >= threshold
