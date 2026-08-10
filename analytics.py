"""
App-usage analytics — event capture.

Records lightweight events about how the app is used (trips created, itineraries
generated, reschedules, packing lists, searches, agent chats, …) into the
Lakebase `app_events` table. This is the SAME write path every other app write
uses (`lakebase.run_write`), so it works reliably as the app's service principal
— no SQL warehouse or Unity Catalog permissions required.

The Spark notebook `notebooks/app_analytics.py` then reads `app_events` via JDBC
and computes analytics into governed **Delta** tables — satisfying the capstone's
"analytics inside a Delta table about your app" requirement (the analytics live
in Delta; the raw events are captured here).

Writes are **fire-and-forget** (a daemon thread) and **best-effort** — a failure
is logged and swallowed, never adding latency to a request or breaking the app.
"""

import logging
import threading
import uuid

import lakebase

logger = logging.getLogger("analytics")


def _write(event_type, trip_id, user_email, detail):
    try:
        lakebase.run_write(
            f"INSERT INTO {lakebase.APP_EVENTS_TABLE} "
            f"(id, event_type, trip_id, user_email, detail) VALUES (%s, %s, %s, %s, %s)",
            (uuid.uuid4().hex, event_type, trip_id, user_email,
             detail[:500] if isinstance(detail, str) else detail),
        )
    except Exception as exc:  # noqa: BLE001 - analytics must never break a request
        logger.warning("log_event(%s) failed: %s", event_type, exc)


def log_event(event_type: str, trip_id: str | None = None,
              user_email: str | None = None, detail: str | None = None) -> None:
    """Record an app event to `app_events`, fire-and-forget (daemon thread)."""
    threading.Thread(
        target=_write, args=(event_type, trip_id, user_email, detail), daemon=True
    ).start()
