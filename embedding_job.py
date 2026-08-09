"""
Shared chunk -> embed -> upsert logic for the Trip Planner's context store.

Gathers the unstructured text the planner reasons over — destination
descriptions (Wikimedia), activity/attraction descriptions + requirements, and
user trip notes — chunks it, embeds it with the shared MiniLM model, and upserts
the vectors into `place_embeddings`.

This is the same code path used by BOTH:
  * POST /embeddings/refresh in app.py (runs in the App's own compute), and
  * notebooks/ingest_place_embeddings.py (the Spark pipeline).

Keeping it here means the Serverless-safe serving copy and the Spark job never
drift. Mirrors the weather app's weather_embedding_job.py.
"""

import os

from psycopg2.extras import execute_values

import embeddings
import lakebase

DEFAULT_CHUNK_SIZE = int(os.environ.get("PLANNER_CHUNK_SIZE", "800"))
DEFAULT_CHUNK_OVERLAP = int(os.environ.get("PLANNER_CHUNK_OVERLAP", "100"))

EMBEDDING_MODEL_NAME = embeddings.EMBEDDING_MODEL_NAME
EMBEDDING_DIM = embeddings.EMBEDDING_DIM


def _combine(*parts: str) -> str:
    """Join non-empty text parts into one embeddable blob."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def fetch_unembedded_sources(
    destinations_table: str = lakebase.DESTINATIONS_TABLE,
    activities_table: str = lakebase.ACTIVITIES_TABLE,
    trips_table: str = lakebase.TRIPS_TABLE,
    embeddings_table: str = lakebase.PLACE_EMBEDDINGS_TABLE,
) -> list[dict]:
    """Return text units (destinations, activities, user notes) that don't yet
    have any row in place_embeddings. A LEFT JOIN ... WHERE e.id IS NULL keeps
    re-runs incremental — only newly added rows get embedded."""
    units: list[dict] = []

    dest_rows = lakebase.run_query(
        f"""
        SELECT d.id, d.trip_id, d.name, d.description
        FROM {destinations_table} d
        LEFT JOIN {embeddings_table} e
               ON e.source_type = 'destination' AND e.source_id = d.id
        WHERE e.id IS NULL AND d.description IS NOT NULL AND d.description <> ''
        """
    )
    for r in dest_rows:
        units.append({
            "source_type": "destination",
            "source_id": r["id"],
            "trip_id": r["trip_id"],
            "text": _combine(r["name"], r["description"]),
        })

    act_rows = lakebase.run_query(
        f"""
        SELECT a.id, a.name, a.description, a.requirements, d.trip_id
        FROM {activities_table} a
        JOIN {destinations_table} d ON d.id = a.destination_id
        LEFT JOIN {embeddings_table} e
               ON e.source_type = 'activity' AND e.source_id = a.id
        WHERE e.id IS NULL
          AND (a.description IS NOT NULL OR a.requirements IS NOT NULL)
        """
    )
    for r in act_rows:
        units.append({
            "source_type": "activity",
            "source_id": r["id"],
            "trip_id": r["trip_id"],
            "text": _combine(r["name"], r["description"], r["requirements"]),
        })

    note_rows = lakebase.run_query(
        f"""
        SELECT t.id, t.notes
        FROM {trips_table} t
        LEFT JOIN {embeddings_table} e
               ON e.source_type = 'user_note' AND e.source_id = t.id
        WHERE e.id IS NULL AND t.notes IS NOT NULL AND t.notes <> ''
        """
    )
    for r in note_rows:
        units.append({
            "source_type": "user_note",
            "source_id": r["id"],
            "trip_id": r["id"],
            "text": r["notes"],
        })

    return [u for u in units if u["text"]]


def build_chunk_rows(units: list[dict], chunk_size: int, chunk_overlap: int) -> list[dict]:
    """Chunk each unit's text and embed all chunks in one batch."""
    chunk_meta: list[dict] = []
    chunk_texts: list[str] = []
    for unit in units:
        for idx, piece in enumerate(embeddings.chunk_text(unit["text"], chunk_size, chunk_overlap)):
            chunk_meta.append({
                "id": f"{unit['source_type']}_{unit['source_id']}_{idx}",
                "source_type": unit["source_type"],
                "source_id": unit["source_id"],
                "trip_id": unit.get("trip_id"),
                "chunk_index": idx,
                "chunk_text": piece,
            })
            chunk_texts.append(piece)

    if not chunk_texts:
        return []

    vectors = embeddings.embed_texts(chunk_texts)
    for meta, vector in zip(chunk_meta, vectors):
        meta["embedding"] = embeddings.to_pgvector_literal(vector)
    return chunk_meta


def write_chunk_embeddings(
    chunk_rows: list[dict], embeddings_table: str = lakebase.PLACE_EMBEDDINGS_TABLE
) -> int:
    """Batch-upsert chunk rows into place_embeddings (idempotent via ON CONFLICT)."""
    if not chunk_rows:
        return 0

    values = [
        (
            r["id"], r["source_type"], r["source_id"], r.get("trip_id"),
            r["chunk_index"], r["chunk_text"], r["embedding"], EMBEDDING_MODEL_NAME,
        )
        for r in chunk_rows
    ]
    sql = f"""
        INSERT INTO {embeddings_table}
            (id, source_type, source_id, trip_id, chunk_index, chunk_text, embedding, model_name)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """
    template = "(%s, %s, %s, %s, %s, %s, %s::vector, %s)"
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, template=template, page_size=100)
            conn.commit()
            return cur.rowcount


def run(chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> dict:
    """End-to-end: ensure tables, find unembedded sources, chunk+embed, upsert.
    Returns a small summary dict (safe to jsonify from the app endpoint)."""
    lakebase.ensure_place_embeddings_table()
    units = fetch_unembedded_sources()
    chunk_rows = build_chunk_rows(units, chunk_size, chunk_overlap) if units else []
    written = write_chunk_embeddings(chunk_rows)
    return {
        "sources_embedded": len(units),
        "chunks_created": len(chunk_rows),
        "rows_written": written,
        "model": EMBEDDING_MODEL_NAME,
    }
