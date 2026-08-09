-- pgvector-backed context-engineering store for the Trip Planner.
--
-- REFERENCE ONLY — lakebase.ensure_place_embeddings_table() creates this
-- idempotently. Holds chunk embeddings of destination descriptions, activity/
-- attraction descriptions + requirements, and user notes, so the agent can
-- retrieve suitable activities by interest + current conditions.
--
-- 384-dim = sentence-transformers/all-MiniLM-L6-v2 (same model/dimension as the
-- news and weather pipelines, so all three stay queryable with the same <=>).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS place_embeddings (
    id          TEXT PRIMARY KEY,                 -- "<source_type>_<source_id>_<chunk_index>"
    source_type TEXT NOT NULL,                    -- 'destination' | 'activity' | 'user_note'
    source_id   TEXT NOT NULL,                    -- destinations.id / activities.id / note id
    trip_id     TEXT,                             -- scopes retrieval to one trip when set
    chunk_index INT NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   VECTOR(384) NOT NULL,
    model_name  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW index for fast cosine similarity search (matches the weather pipeline).
CREATE INDEX IF NOT EXISTS idx_place_embeddings_embedding
    ON place_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_place_embeddings_source
    ON place_embeddings (source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_place_embeddings_trip_id
    ON place_embeddings (trip_id);

-- Verify
SELECT table_name, column_name, data_type, udt_name
FROM information_schema.columns
WHERE table_name = 'place_embeddings'
ORDER BY ordinal_position;
