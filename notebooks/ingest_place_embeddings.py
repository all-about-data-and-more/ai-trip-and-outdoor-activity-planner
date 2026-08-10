# Databricks notebook source
# MAGIC %md
# MAGIC # Place Embeddings — Spark Pipeline (Trip Planner)
# MAGIC
# MAGIC The capstone's **Spark data pipeline**. It takes the unstructured text the
# MAGIC planner reasons over — destination descriptions (Wikimedia), activity /
# MAGIC attraction descriptions, and user trip notes — and computes vector
# MAGIC embeddings **at scale with Spark**, writing a governed Delta "gold" table
# MAGIC in Unity Catalog.
# MAGIC
# MAGIC ```
# MAGIC  raw place documents (Delta bronze) ──Spark DataFrame──▶ chunk (explode)
# MAGIC        │                                                     │
# MAGIC        │                                          pandas_udf: MiniLM embed
# MAGIC        ▼                                                     ▼
# MAGIC  (materialized from Lakebase)                     place_embeddings_gold (Delta)
# MAGIC                                                             │
# MAGIC                                              POST /embeddings/refresh (App)
# MAGIC                                                             ▼
# MAGIC                                                Lakebase place_embeddings (pgvector, serving)
# MAGIC ```
# MAGIC
# MAGIC **Why the Lakebase serving copy is written by the App, not here:** on this
# MAGIC workspace's Serverless-only compute, `psycopg2` crashes (SIGABRT) and Spark's
# MAGIC JDBC writer isn't supported against Lakebase — the exact constraint documented
# MAGIC in the weather app. So Spark does the heavy, governed batch embedding into
# MAGIC **Delta** (which is Serverless-native), and the App's `POST /embeddings/refresh`
# MAGIC does the lightweight pgvector serving write (App compute has no psycopg2
# MAGIC restriction). See `docs/spark_pipeline_and_serverless.md` for the full story.

# COMMAND ----------

# MAGIC %pip install -q sentence-transformers
# MAGIC %restart_python

# COMMAND ----------

import pandas as pd
from pyspark.sql import functions as F
from pyspark.sql import Window
from pyspark.sql.types import ArrayType, FloatType, StringType

CATALOG = "workspace"                       # change to your Unity Catalog
SCHEMA = "trip_planner"
BRONZE_TABLE = f"{CATALOG}.{SCHEMA}.raw_place_documents"    # source text
GOLD_TABLE = f"{CATALOG}.{SCHEMA}.place_embeddings_gold"    # embeddings output

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
CHUNK_SIZE, CHUNK_OVERLAP = 800, 100

spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Source documents (Delta bronze)
# MAGIC
# MAGIC In production this bronze table is materialized from Lakebase (e.g. via
# MAGIC Lakebase **Change Data Feed** into Unity Catalog, per the Day-2 README, or a
# MAGIC scheduled export of `destinations` / `activities` / `trips.notes`). So the
# MAGIC notebook always runs and demonstrates the Spark path even before that sync is
# MAGIC wired, we seed a tiny sample if the bronze table isn't there yet.

# COMMAND ----------

if not spark.catalog.tableExists(BRONZE_TABLE):
    sample = spark.createDataFrame(pd.DataFrame([
        {"source_type": "destination", "source_id": "d1", "trip_id": "t1",
         "text": "Kyoto is a Japanese city famous for its classical Buddhist temples, "
                 "gardens, imperial palaces, Shinto shrines and traditional wooden houses."},
        {"source_type": "activity", "source_id": "a1", "trip_id": "t1",
         "text": "Fushimi Inari Shrine: an outdoor mountain trail lined with thousands of vermilion torii gates."},
        {"source_type": "activity", "source_id": "a2", "trip_id": "t1",
         "text": "Kyoto National Museum: an indoor museum of pre-modern Japanese art, ideal on a rainy day."},
        {"source_type": "user_note", "source_id": "t1", "trip_id": "t1",
         "text": "We love hiking and food markets and want to avoid big crowds."},
    ]))
    sample.write.mode("overwrite").saveAsTable(BRONZE_TABLE)
    print(f"Seeded sample bronze table {BRONZE_TABLE}")

docs = spark.table(BRONZE_TABLE)
print(f"{docs.count()} source documents")
docs.show(truncate=80)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Chunk with Spark (overlapping character windows)
# MAGIC
# MAGIC A Spark UDF splits each document into overlapping chunks, then `explode`
# MAGIC turns the array into one row per chunk — the same sliding-window scheme as
# MAGIC the app's `embeddings.chunk_text`, done in the DataFrame API so it scales.

# COMMAND ----------

@F.udf(ArrayType(StringType()))
def chunk_udf(text):
    if not text:
        return []
    step = max(CHUNK_SIZE - CHUNK_OVERLAP, 1)
    out = []
    for start in range(0, len(text), step):
        piece = text[start:start + CHUNK_SIZE].strip()
        if piece:
            out.append(piece)
        if start + CHUNK_SIZE >= len(text):
            break
    return out

chunks = (
    docs
    .withColumn("chunk_text", F.explode(chunk_udf(F.col("text"))))
    .withColumn("chunk_index",
                F.row_number().over(
                    Window.partitionBy("source_type", "source_id")
                    .orderBy(F.monotonically_increasing_id())) - 1)
    .select("source_type", "source_id", "trip_id", "chunk_index", "chunk_text")
)
print(f"{chunks.count()} chunks")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Embed with a pandas UDF (distributed)
# MAGIC
# MAGIC A `pandas_udf` loads the sentence-transformers model **once per executor**
# MAGIC (module-level cache) and embeds each partition as a batch — Spark distributes
# MAGIC the work across the cluster. Output is a 384-float array per chunk.

# COMMAND ----------

# DBTITLE 1,Cell 9
import os

_model = None

def _get_model():
    global _model
    if _model is None:
        # Set cache to /tmp (writable on Serverless executors)
        os.environ['HF_HOME'] = '/tmp/huggingface_cache'
        os.environ['TRANSFORMERS_CACHE'] = '/tmp/huggingface_cache'
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _model

@F.pandas_udf(ArrayType(FloatType()))
def embed_udf(texts: pd.Series) -> pd.Series:
    vectors = _get_model().encode(texts.tolist(), show_progress_bar=False)
    return pd.Series([v.tolist() for v in vectors])

embedded = (
    chunks
    .withColumn("embedding", embed_udf(F.col("chunk_text")))
    .withColumn("model_name", F.lit(EMBEDDING_MODEL_NAME))
    .withColumn("id", F.concat_ws("_", "source_type", "source_id", "chunk_index"))
    .withColumn("created_at", F.current_timestamp())
)
embedded.select("id", "source_type", "chunk_index", "chunk_text").show(5, truncate=60)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Write the governed Delta gold table
# MAGIC
# MAGIC `saveAsTable` writes a managed Delta table in Unity Catalog (Serverless-safe).
# MAGIC This is the batch, governed copy of the embeddings; the App then syncs it into
# MAGIC Lakebase `place_embeddings` (pgvector) for low-latency similarity search.

# COMMAND ----------

(embedded
    .select("id", "source_type", "source_id", "trip_id", "chunk_index",
            "chunk_text", "embedding", "model_name", "created_at")
    .write.mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable(GOLD_TABLE))

print(f"Wrote {spark.table(GOLD_TABLE).count()} embedded chunks to {GOLD_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Serving copy → Lakebase pgvector
# MAGIC
# MAGIC Call the deployed App's `POST /embeddings/refresh` (or run `embedding_job.py`
# MAGIC locally) to load the vectors into Lakebase `place_embeddings`, where the
# MAGIC agent's `search_activities` retrieval runs. That path uses psycopg2 on App
# MAGIC compute (no Serverless restriction), keeping this Spark job Serverless-safe.