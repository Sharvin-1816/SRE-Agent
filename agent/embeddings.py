"""
agent/embeddings.py

Generates vector embeddings using HuggingFaceEmbeddings from langchain-huggingface.
Model: all-MiniLM-L6-v2 (384 dimensions, runs locally, no API calls)
"""

import numpy as np
from rich.console import Console

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