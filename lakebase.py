"""
Lakebase (Databricks-managed Postgres) connection helper + schema for the
AI Trip & Outdoor Activity Planner.

Connection: a single Postgres URL
(postgresql://role:password@host:5432/databricks_postgres?sslmode=require),
resolved in this order so the same code runs on Databricks, locally, or in a
notebook:
  1. LAKEBASE_URL env var (preferred for local dev / any non-Databricks box), else
  2. a base64-encoded Databricks secret (scope `database`, key `lakebase-url`),
     read via WorkspaceClient — needs the Databricks CLI configured locally.
Both are normalized through _maybe_decode() because the value can arrive base64-
encoded (once by setup_secrets.py, and again when Databricks injects a
secret-backed env var).

Schema: the seven capstone tables (users, trips, destinations, activities,
itinerary_items, weather_snapshots, packing_items) plus a pgvector-backed
`place_embeddings` table. Each has an idempotent `ensure_*_table()` helper.
Index/extension DDL runs BEST-EFFORT (see _try_ddl): if the tables were created
by a different Postgres role, the app's role can't add indexes to them
("must be owner"), and that must not abort a request — the tables + indexes
already exist in that case. Reference DDL also lives in `sql/`.
"""

import base64
import logging
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger("lakebase")

# --- Table names (overridable via env, but you rarely need to) --------------
USERS_TABLE = os.environ.get("USERS_TABLE", "users")
TRIPS_TABLE = os.environ.get("TRIPS_TABLE", "trips")
DESTINATIONS_TABLE = os.environ.get("DESTINATIONS_TABLE", "destinations")
ACTIVITIES_TABLE = os.environ.get("ACTIVITIES_TABLE", "activities")
ITINERARY_ITEMS_TABLE = os.environ.get("ITINERARY_ITEMS_TABLE", "itinerary_items")
WEATHER_SNAPSHOTS_TABLE = os.environ.get("WEATHER_SNAPSHOTS_TABLE", "weather_snapshots")
PACKING_ITEMS_TABLE = os.environ.get("PACKING_ITEMS_TABLE", "packing_items")
PLACE_EMBEDDINGS_TABLE = os.environ.get("PLACE_EMBEDDINGS_TABLE", "place_embeddings")

# 384 = sentence-transformers/all-MiniLM-L6-v2 output size (see embeddings.py).
EMBEDDING_DIM = int(os.environ.get("PLANNER_EMBEDDING_DIM", "384"))

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


def _looks_like_pg_url(s: str) -> bool:
    return s.startswith("postgres://") or s.startswith("postgresql://")


def _maybe_decode(value: str) -> str:
    """Return a usable Postgres URL whether `value` is a raw URL or base64-
    encoded one or more times.

    The value can be encoded TWICE: setup_secrets.py stores the secret
    base64-encoded, and Databricks base64-encodes it again when injecting a
    secret-backed env var (LAKEBASE_URL) into the app. So peel base64 layers
    until we hit a postgresql:// URL, capping the loop to stay safe. If no URL
    surfaces, return the original untouched.
    """
    original = (value or "").strip()
    v = original
    for _ in range(4):  # raw / single / double / triple-encoded all covered
        if _looks_like_pg_url(v):
            return v
        try:
            nxt = base64.b64decode(v).decode("utf-8").strip()
        except Exception:  # noqa: BLE001 - not base64; stop peeling
            return original
        if not nxt or nxt == v:
            return original
        v = nxt
    return original


def _lakebase_url() -> str:
    """Resolve the Lakebase connection URL: LAKEBASE_URL env first (portable),
    then the Databricks secret. Both are normalized through _maybe_decode."""
    url = os.environ.get("LAKEBASE_URL")
    if url:
        return _maybe_decode(url)

    # Imported lazily so the env-var path doesn't require databricks-sdk.
    from databricks.sdk import WorkspaceClient

    secret = WorkspaceClient().secrets.get_secret(scope=_SCOPE, key=_KEY)
    return _maybe_decode(secret.value)


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE and return the affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


def _try_ddl(sql: str) -> None:
    """Run a CREATE INDEX / CREATE EXTENSION statement best-effort.

    If the app's Postgres role doesn't OWN the target table (e.g. the tables
    were created by another role via the SQL editor), it can't add an index and
    Postgres raises "must be owner of table ...". Indexes/extensions are
    optimizations, and in that scenario they already exist — so log and continue
    rather than aborting the whole request. (Same defensive pattern used for the
    Day-2 chunk-embeddings table.)
    """
    try:
        run_write(sql)
    except psycopg2.Error as exc:
        logger.warning("Skipping DDL (best-effort): %s", str(exc).strip())


# ---------------------------------------------------------------------------
# Schema — one idempotent ensure_* helper per table, created in FK order.
# CREATE TABLE runs via run_write (a no-op if the table already exists, even
# for a non-owner); indexes/extensions run via _try_ddl (best-effort).
# ---------------------------------------------------------------------------
def ensure_users_table(table: str = USERS_TABLE) -> None:
    run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            email         TEXT PRIMARY KEY,
            name          TEXT,
            home_location TEXT,
            preferences   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def ensure_trips_table(table: str = TRIPS_TABLE) -> None:
    run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id         TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            title      TEXT NOT NULL,
            start_date DATE,
            end_date   DATE,
            notes      TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_user_email ON {table} (user_email)")


def ensure_destinations_table(table: str = DESTINATIONS_TABLE) -> None:
    run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id           TEXT PRIMARY KEY,
            trip_id      TEXT NOT NULL,
            name         TEXT NOT NULL,
            lat          DOUBLE PRECISION,
            lon          DOUBLE PRECISION,
            description  TEXT,
            wiki_url     TEXT,
            arrival_date DATE,
            depart_date  DATE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_trip_id ON {table} (trip_id)")


def ensure_activities_table(table: str = ACTIVITIES_TABLE) -> None:
    run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id             TEXT PRIMARY KEY,
            destination_id TEXT NOT NULL,
            name           TEXT NOT NULL,
            category       TEXT,
            is_outdoor     BOOLEAN NOT NULL DEFAULT FALSE,
            description    TEXT,
            requirements   TEXT,
            tags           JSONB NOT NULL DEFAULT '[]'::jsonb,
            source         TEXT NOT NULL DEFAULT 'user',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_destination_id ON {table} (destination_id)")
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_is_outdoor ON {table} (is_outdoor)")


def ensure_itinerary_items_table(table: str = ITINERARY_ITEMS_TABLE) -> None:
    run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id             TEXT PRIMARY KEY,
            trip_id        TEXT NOT NULL,
            destination_id TEXT,
            activity_id    TEXT,
            title          TEXT NOT NULL,
            day_date       DATE,
            start_time     TEXT,
            end_time       TEXT,
            is_outdoor     BOOLEAN NOT NULL DEFAULT FALSE,
            status         TEXT NOT NULL DEFAULT 'planned',
            notes          TEXT,
            sort_order     INT NOT NULL DEFAULT 0,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_trip_id ON {table} (trip_id)")
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_day_date ON {table} (day_date)")


def ensure_weather_snapshots_table(table: str = WEATHER_SNAPSHOTS_TABLE) -> None:
    run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id                TEXT PRIMARY KEY,
            destination_id    TEXT NOT NULL,
            snapshot_date     DATE NOT NULL,
            temp_high         DOUBLE PRECISION,
            temp_low          DOUBLE PRECISION,
            precip_chance_pct DOUBLE PRECISION,
            precip_amount     DOUBLE PRECISION,
            wind_max          DOUBLE PRECISION,
            aqi               DOUBLE PRECISION,
            pm2_5             DOUBLE PRECISION,
            uv_index_max      DOUBLE PRECISION,
            pollen            JSONB,
            conditions        TEXT,
            payload           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _try_ddl(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_dest_date "
        f"ON {table} (destination_id, snapshot_date)"
    )


def ensure_packing_items_table(table: str = PACKING_ITEMS_TABLE) -> None:
    run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id         TEXT PRIMARY KEY,
            trip_id    TEXT NOT NULL,
            category   TEXT,
            item       TEXT NOT NULL,
            reason     TEXT,
            quantity   INT NOT NULL DEFAULT 1,
            packed     BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_trip_id ON {table} (trip_id)")


def ensure_place_embeddings_table(
    table: str = PLACE_EMBEDDINGS_TABLE, embedding_dim: int = EMBEDDING_DIM
) -> None:
    """pgvector-backed chunk embeddings for context engineering. `source_type`
    is 'destination' | 'activity' | 'user_note' and `source_id` points at the
    originating row. Polymorphic on purpose, so it's not FK-constrained."""
    _try_ddl("CREATE EXTENSION IF NOT EXISTS vector")
    run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id          TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            trip_id     TEXT,
            chunk_index INT NOT NULL,
            chunk_text  TEXT NOT NULL,
            embedding   VECTOR({embedding_dim}) NOT NULL,
            model_name  TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _try_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_embedding "
        f"ON {table} USING hnsw (embedding vector_cosine_ops)"
    )
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_source ON {table} (source_type, source_id)")
    _try_ddl(f"CREATE INDEX IF NOT EXISTS idx_{table}_trip_id ON {table} (trip_id)")


def ensure_all_tables() -> None:
    """Create every table in dependency order. Idempotent — safe to call on
    each request/startup."""
    ensure_users_table()
    ensure_trips_table()
    ensure_destinations_table()
    ensure_activities_table()
    ensure_itinerary_items_table()
    ensure_weather_snapshots_table()
    ensure_packing_items_table()
    ensure_place_embeddings_table()
