# The Spark Pipeline &amp; the Serverless Constraint

## The short version

Embeddings are computed **two** ways that never drift, because both call the same chunking logic:

1. **Batch / governed** — `notebooks/ingest_place_embeddings.py` runs a real Spark job (DataFrame +
   `pandas_udf`) and writes a **Delta gold** table in Unity Catalog. This is the mandatory Spark
   pipeline, and it is **Serverless-safe**.
2. **Serving** — `POST /embeddings/refresh` (app) / `embedding_job.py` (local) writes the vectors into
   **Lakebase `place_embeddings`** (pgvector), where the agent's retrieval runs.

## Why the split — the honest reason

On this workspace's **Databricks Free-edition Serverless** compute:

- **`psycopg2` crashes (SIGABRT)** when run inside a Serverless notebook/Job, and
- **Spark's JDBC writer isn't supported** against the Lakebase instance.

This is the exact constraint documented in the bootcamp's weather app
(`notebooks/ingest_weather_embeddings.py`). So we don't fight it:

- **Spark → Delta** works perfectly on Serverless (Delta is native), so the heavy, distributed,
  governed batch embedding lives there. That satisfies the "Spark data pipeline" requirement with a
  genuine distributed job — window functions, a Spark UDF for chunking, and a `pandas_udf` that loads
  MiniLM once per executor and embeds each partition as a batch.
- **App → Lakebase** is where the low-latency serving vectors go. **App compute has no psycopg2
  restriction**, so `POST /embeddings/refresh` (which calls `embedding_job.py`) writes pgvector rows
  reliably. You can also run `embedding_job.py` locally.

## The medallion flow

```
Lakebase (destinations/activities/trips.notes)
      │  (materialize to Delta bronze — Lakebase Change Data Feed, or a scheduled export)
      ▼
Delta bronze: raw_place_documents
      │  Spark: chunk (UDF+explode) → embed (pandas_udf MiniLM)
      ▼
Delta gold: place_embeddings_gold        ← the Spark pipeline's output (governed, Serverless-safe)
      │  POST /embeddings/refresh  (App compute; psycopg2 OK here)
      ▼
Lakebase place_embeddings (pgvector)      ← serving; the agent's search_activities queries this
```

> The notebook seeds a tiny sample bronze table if none exists, so it always runs and demonstrates the
> Spark path even before the Lakebase→Delta sync is wired. In production, populate
> `raw_place_documents` from Lakebase (Change Data Feed into Unity Catalog is the clean option — see
> the Day-2 README's CDF section).

## If you're on a workspace WITHOUT the Serverless-only constraint

If your workspace has classic clusters (or a Lakebase instance that accepts Spark JDBC / psycopg2 on
its compute), you can write pgvector directly from the Spark job instead of via the app endpoint. The
chunking + embedding code is identical; only the final write target changes. The two-step split here
is the portable, Free-edition-safe default.
