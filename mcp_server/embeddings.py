"""
Shared sentence-transformers embedding helpers for the Trip Planner.

Both the Spark ingestion pipeline (notebooks/ingest_place_embeddings.py, via
embedding_job.py) and the retrieval path (planner_tools.search_activities) must
embed text with the exact same model and format vectors the same way for
pgvector, so that logic lives here once. Ported from the weather app's
embeddings.py — same model, same 384-dim convention, so every embedding table
in the bootcamp stays queryable with the same `<=>` operator.
"""

import os

EMBEDDING_MODEL_NAME = os.environ.get(
    "PLANNER_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
# all-MiniLM-L6-v2 emits 384-dim vectors. If you swap PLANNER_EMBEDDING_MODEL,
# update PLANNER_EMBEDDING_DIM (lakebase.EMBEDDING_DIM) to match its output size.
EMBEDDING_DIM = int(os.environ.get("PLANNER_EMBEDDING_DIM", "384"))

_model = None


def get_model():
    """Load and cache the sentence-transformers model once per process."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings, returning one vector (as a plain list) per input."""
    vectors = get_model().encode(texts, show_progress_bar=False)
    return [v.tolist() for v in vectors]


def to_pgvector_literal(vector: list[float]) -> str:
    """Format a vector as pgvector's '[v1,v2,...]' literal (square brackets, not
    Postgres array curly braces — pgvector rejects '{...}')."""
    return "[" + ",".join(str(float(x)) for x in vector) + "]"


def chunk_text(text: str, chunk_size: int = 800, chunk_overlap: int = 100) -> list[str]:
    """Split text into overlapping character-window chunks (same sliding-window
    convention as the news/weather pipelines: 800/100 by default)."""
    if not text:
        return []

    step = max(chunk_size - chunk_overlap, 1)
    chunks = []
    for start in range(0, len(text), step):
        piece = text[start : start + chunk_size].strip()
        if piece:
            chunks.append(piece)
        if start + chunk_size >= len(text):
            break
    return chunks
