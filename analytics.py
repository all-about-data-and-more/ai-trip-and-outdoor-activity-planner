"""
App-usage analytics — written to a Unity Catalog **Delta** table.

Captures lightweight events about how the app is used (trips created, itineraries
generated, reschedules, packing lists, searches, agent chats, ...) into a
governed Delta table via the Databricks **SQL Statement Execution API**, so a
Spark notebook (`notebooks/app_analytics.py`) can compute analytics ABOUT the
app. This satisfies the capstone's "analytics inside a Delta table about your
app" requirement.

Design notes:
  * Writes are **fire-and-forget** (a daemon thread) and **best-effort** — if the
    warehouse is unavailable or a write fails, it's logged and swallowed, never
    adding latency to a request or breaking the app.
  * Delta is written via a SQL warehouse (not psycopg2/Spark), so this works from
    a Serverless Databricks App. The warehouse id is auto-discovered, or set
    DATABRICKS_WAREHOUSE_ID to pin one.
"""

import logging
import os
import threading
import uuid

logger = logging.getLogger("analytics")

CATALOG = os.environ.get("ANALYTICS_CATALOG", "workspace")
SCHEMA = os.environ.get("ANALYTICS_SCHEMA", "trip_planner")
EVENTS_TABLE = f"{CATALOG}.{SCHEMA}.app_events"

_client_singleton = None
_warehouse_id = None
_table_ready = False
_lock = threading.Lock()


def _client():
    global _client_singleton
    if _client_singleton is None:
        from databricks.sdk import WorkspaceClient
        _client_singleton = WorkspaceClient()
    return _client_singleton


def _warehouse():
    """Resolve a SQL warehouse id (env override, else the first available)."""
    global _warehouse_id
    if _warehouse_id:
        return _warehouse_id
    env = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if env:
        _warehouse_id = env
        return _warehouse_id
    try:
        whs = list(_client().warehouses.list())
        running = [w for w in whs if getattr(w.state, "value", None) == "RUNNING"]
        _warehouse_id = (running or whs)[0].id if whs else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("No SQL warehouse available for analytics: %s", exc)
        _warehouse_id = None
    return _warehouse_id


def _exec(statement, parameters=None):
    wh = _warehouse()
    if not wh:
        return None
    return _client().statement_execution.execute_statement(
        warehouse_id=wh, statement=statement, parameters=parameters,
        catalog=CATALOG, wait_timeout="30s",
    )


def ensure_events_table():
    """Create the analytics schema + Delta events table once (best-effort)."""
    global _table_ready
    with _lock:
        if _table_ready:
            return
        try:
            _exec(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
            _exec(
                f"""CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                    event_id   STRING,
                    event_type STRING,
                    trip_id    STRING,
                    user_email STRING,
                    detail     STRING,
                    created_at TIMESTAMP
                ) USING DELTA"""
            )
            _table_ready = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("ensure_events_table failed: %s", exc)


def _write(event_type, trip_id, user_email, detail):
    try:
        ensure_events_table()
        if not _table_ready:
            return
        from databricks.sdk.service.sql import StatementParameterListItem as P
        _exec(
            f"INSERT INTO {EVENTS_TABLE} "
            f"(event_id, event_type, trip_id, user_email, detail, created_at) "
            f"VALUES (:id, :etype, :trip, :user, :detail, current_timestamp())",
            parameters=[
                P(name="id", value=uuid.uuid4().hex),
                P(name="etype", value=event_type),
                P(name="trip", value=trip_id),
                P(name="user", value=user_email),
                P(name="detail", value=detail),
            ],
        )
    except Exception as exc:  # noqa: BLE001 - analytics must never break a request
        logger.warning("log_event(%s) failed: %s", event_type, exc)


def log_event(event_type: str, trip_id: str | None = None,
              user_email: str | None = None, detail: str | None = None) -> None:
    """Record an app event to the Delta table, fire-and-forget (daemon thread)."""
    threading.Thread(
        target=_write, args=(event_type, trip_id, user_email, detail), daemon=True
    ).start()
