"""
Shared tool layer for the Trip Planner — the single source of truth that BOTH
agent surfaces call:
  * the in-app chat agent (agent.py / POST /agent/chat), and
  * the FastMCP server (mcp_server/planner_mcp_server.py) for Agent Bricks.

Every function returns a clean, JSON-serializable dict and does its own
Lakebase reads/writes + third-party calls, so the app routes and the MCP tools
stay thin. This module is the "retrieves AND writes" agent capability the
capstone requires.

Phase 1 (this file): user/trip/destination management, weather + air-quality
reads and snapshots, and context-engineering retrieval (search_activities).
Phase 2 extends it with generate_itinerary, reschedule_for_weather,
add/remove/move_itinerary_item, build_packing_list, and explain_changes.
"""

import json
import os
import uuid
from datetime import date, datetime, timedelta

import embeddings
import lakebase
from clients import air_quality_client, geocoding_client, weather_client, wikimedia_client

# Reschedule / advisory thresholds (imperial defaults; overridable via env).
RAIN_PRECIP_CHANCE_PCT = float(os.environ.get("RAIN_PRECIP_CHANCE_PCT", "50"))
RAIN_PRECIP_AMOUNT = float(os.environ.get("RAIN_PRECIP_AMOUNT", "0.1"))
AQI_UNHEALTHY_THRESHOLD = float(os.environ.get("AQI_UNHEALTHY_THRESHOLD", "100"))

# Keyword heuristics for classifying an attraction as outdoor (drives
# weather-based rescheduling). Deliberately simple + explainable.
_OUTDOOR_HINTS = (
    "park", "garden", "trail", "hike", "hiking", "mountain", "lake", "beach",
    "river", "falls", "waterfall", "zoo", "botanical", "forest", "island",
    "bridge", "square", "plaza", "market", "harbor", "harbour", "bay", "cliff",
    "valley", "canyon", "vineyard", "outdoor", "monument", "castle", "ruins",
)
_INDOOR_HINTS = (
    "museum", "gallery", "theatre", "theater", "aquarium", "library",
    "cathedral", "church", "temple", "shrine", "mosque", "hall", "centre",
    "center", "mall", "palace interior", "planetarium", "observatory",
)


def _new_id() -> str:
    return uuid.uuid4().hex


def _guess_outdoor(name: str, text: str | None) -> bool:
    """Best-effort indoor/outdoor classification from the title + description."""
    blob = f"{name or ''} {text or ''}".lower()
    if any(h in blob for h in _INDOOR_HINTS):
        return False
    return any(h in blob for h in _OUTDOOR_HINTS)


# ---------------------------------------------------------------------------
# Users & trips
# ---------------------------------------------------------------------------
def ensure_user(email: str, name: str | None = None, home_location: str | None = None) -> dict:
    """Insert the user row if absent (idempotent). Returns the user."""
    lakebase.ensure_users_table()
    lakebase.run_write(
        f"""
        INSERT INTO {lakebase.USERS_TABLE} (email, name, home_location)
        VALUES (%s, %s, %s)
        ON CONFLICT (email) DO UPDATE
            SET name = COALESCE(EXCLUDED.name, {lakebase.USERS_TABLE}.name),
                home_location = COALESCE(EXCLUDED.home_location, {lakebase.USERS_TABLE}.home_location)
        """,
        (email, name, home_location),
    )
    rows = lakebase.run_query(
        f"SELECT * FROM {lakebase.USERS_TABLE} WHERE email = %s", (email,)
    )
    return rows[0] if rows else {"email": email}


def create_trip(user_email: str, title: str, start_date=None, end_date=None, notes=None) -> dict:
    """Create a trip for a user and return it."""
    lakebase.ensure_trips_table()
    ensure_user(user_email)
    trip_id = _new_id()
    lakebase.run_write(
        f"""
        INSERT INTO {lakebase.TRIPS_TABLE} (id, user_email, title, start_date, end_date, notes)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (trip_id, user_email, title, start_date or None, end_date or None, notes),
    )
    return get_trip(trip_id)


def get_trip(trip_id: str) -> dict:
    """Return one trip with its destinations, or {'status':'error'} if missing."""
    rows = lakebase.run_query(f"SELECT * FROM {lakebase.TRIPS_TABLE} WHERE id = %s", (trip_id,))
    if not rows:
        return {"status": "error", "message": f"Trip {trip_id!r} not found."}
    trip = rows[0]
    trip["destinations"] = list_destinations(trip_id)
    return trip


def list_trips(user_email: str) -> list[dict]:
    lakebase.ensure_trips_table()
    return lakebase.run_query(
        f"SELECT * FROM {lakebase.TRIPS_TABLE} WHERE user_email = %s ORDER BY created_at DESC",
        (user_email,),
    )


def delete_trip(trip_id: str) -> int:
    return lakebase.run_write(f"DELETE FROM {lakebase.TRIPS_TABLE} WHERE id = %s", (trip_id,))


# ---------------------------------------------------------------------------
# Destinations & activities (Wikimedia enrichment)
# ---------------------------------------------------------------------------
def add_destination(
    trip_id: str, name: str, arrival_date=None, depart_date=None, max_attractions: int = 10
) -> dict:
    """
    Geocode a destination, pull its Wikimedia description, seed nearby
    attractions as `activities`, and persist everything. This is a WRITE tool:
    it inserts one `destinations` row and up to `max_attractions` `activities`
    rows. Returns the created destination with its activities.
    """
    lakebase.ensure_destinations_table()
    lakebase.ensure_activities_table()

    loc = geocoding_client.geocode(name)

    # Wikimedia description (best-effort — a missing page shouldn't fail the save).
    description, wiki_url = None, None
    try:
        summary = wikimedia_client.get_summary(loc["name"])
        description, wiki_url = summary.get("extract"), summary.get("url")
    except Exception:  # noqa: BLE001
        pass

    dest_id = _new_id()
    lakebase.run_write(
        f"""
        INSERT INTO {lakebase.DESTINATIONS_TABLE}
            (id, trip_id, name, lat, lon, description, wiki_url, arrival_date, depart_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            dest_id, trip_id, geocoding_client.place_label(loc), loc["lat"], loc["lon"],
            description, wiki_url, arrival_date or None, depart_date or None,
        ),
    )

    attractions = wikimedia_client.get_nearby_attractions(
        loc["lat"], loc["lon"], limit=max_attractions
    )
    created_activities = []
    for att in attractions:
        act_id = _new_id()
        is_outdoor = _guess_outdoor(att["title"], att.get("extract"))
        lakebase.run_write(
            f"""
            INSERT INTO {lakebase.ACTIVITIES_TABLE}
                (id, destination_id, name, category, is_outdoor, description, tags, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'wikimedia')
            """,
            (
                act_id, dest_id, att["title"],
                "outdoor" if is_outdoor else "indoor", is_outdoor,
                att.get("extract"), json.dumps([]),
            ),
        )
        created_activities.append({"id": act_id, "name": att["title"], "is_outdoor": is_outdoor})

    return {
        "id": dest_id,
        "trip_id": trip_id,
        "name": geocoding_client.place_label(loc),
        "lat": loc["lat"],
        "lon": loc["lon"],
        "description": description,
        "wiki_url": wiki_url,
        "activities_created": len(created_activities),
        "activities": created_activities,
    }


def list_destinations(trip_id: str) -> list[dict]:
    return lakebase.run_query(
        f"SELECT * FROM {lakebase.DESTINATIONS_TABLE} WHERE trip_id = %s ORDER BY arrival_date NULLS LAST, created_at",
        (trip_id,),
    )


def list_activities(destination_id: str) -> list[dict]:
    return lakebase.run_query(
        f"SELECT * FROM {lakebase.ACTIVITIES_TABLE} WHERE destination_id = %s ORDER BY name",
        (destination_id,),
    )


def add_user_activity(destination_id: str, name: str, description=None, requirements=None,
                      is_outdoor: bool = False, category: str | None = None) -> dict:
    """Let a user add their own activity (also embeddable + retrievable)."""
    lakebase.ensure_activities_table()
    act_id = _new_id()
    lakebase.run_write(
        f"""
        INSERT INTO {lakebase.ACTIVITIES_TABLE}
            (id, destination_id, name, category, is_outdoor, description, requirements, tags, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'user')
        """,
        (act_id, destination_id, name, category or ("outdoor" if is_outdoor else "indoor"),
         is_outdoor, description, requirements, json.dumps([])),
    )
    return {"id": act_id, "destination_id": destination_id, "name": name, "is_outdoor": is_outdoor}


# ---------------------------------------------------------------------------
# Weather + air quality (reads and snapshots)
# ---------------------------------------------------------------------------
def get_forecast(location: str, days: int = 7) -> dict:
    """Daily weather forecast for a place name or 'lat,lon' (Open-Meteo)."""
    try:
        return weather_client.get_forecast(location, days)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Could not fetch forecast: {e}"}


def get_air_quality(location: str, days: int = 5) -> dict:
    """Per-day AQI / PM / UV / pollen for a place name or 'lat,lon' (Open-Meteo)."""
    try:
        return air_quality_client.get_air_quality(location, days)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Could not fetch air quality: {e}"}


def snapshot_weather(destination_id: str, days: int = 7) -> dict:
    """
    Fetch forecast + air quality for a destination and upsert one
    `weather_snapshots` row per day (WRITE). Returns the snapshots. This is what
    the reschedule logic reads.
    """
    lakebase.ensure_weather_snapshots_table()
    dest_rows = lakebase.run_query(
        f"SELECT * FROM {lakebase.DESTINATIONS_TABLE} WHERE id = %s", (destination_id,)
    )
    if not dest_rows:
        return {"status": "error", "message": f"Destination {destination_id!r} not found."}
    dest = dest_rows[0]
    coords = f"{dest['lat']},{dest['lon']}" if dest.get("lat") is not None else dest["name"]

    forecast = weather_client.get_forecast(coords, days)
    air = air_quality_client.get_air_quality(coords, min(days, 7))
    air_by_date = {d["date"]: d for d in air.get("days", [])}

    snapshots = []
    for day in forecast.get("days", []):
        d = day["date"]
        aq = air_by_date.get(d, {})
        snap_id = f"{destination_id}_{d}"
        lakebase.run_write(
            f"""
            INSERT INTO {lakebase.WEATHER_SNAPSHOTS_TABLE}
                (id, destination_id, snapshot_date, temp_high, temp_low, precip_chance_pct,
                 precip_amount, wind_max, aqi, pm2_5, uv_index_max, pollen, conditions, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (destination_id, snapshot_date) DO UPDATE SET
                temp_high = EXCLUDED.temp_high, temp_low = EXCLUDED.temp_low,
                precip_chance_pct = EXCLUDED.precip_chance_pct, precip_amount = EXCLUDED.precip_amount,
                wind_max = EXCLUDED.wind_max, aqi = EXCLUDED.aqi, pm2_5 = EXCLUDED.pm2_5,
                uv_index_max = EXCLUDED.uv_index_max, pollen = EXCLUDED.pollen,
                conditions = EXCLUDED.conditions, payload = EXCLUDED.payload, fetched_at = now()
            """,
            (
                snap_id, destination_id, d, day.get("temp_high"), day.get("temp_low"),
                day.get("precip_chance_pct"), day.get("precip_amount"), day.get("wind_max"),
                aq.get("aqi"), aq.get("pm2_5"), aq.get("uv_index_max"),
                json.dumps(aq.get("pollen")) if aq.get("pollen") else None,
                day.get("conditions"), json.dumps({"forecast": day, "air": aq}),
            ),
        )
        snapshots.append({
            "date": d, "conditions": day.get("conditions"),
            "temp_high": day.get("temp_high"), "temp_low": day.get("temp_low"),
            "precip_chance_pct": day.get("precip_chance_pct"), "aqi": aq.get("aqi"),
        })

    return {"destination_id": destination_id, "location": dest["name"], "snapshots": snapshots}


# ---------------------------------------------------------------------------
# Context-engineering retrieval
# ---------------------------------------------------------------------------
def search_activities(query: str, trip_id: str | None = None, top_k: int = 8,
                      outdoor_only: bool | None = None) -> dict:
    """
    Semantic search over embedded destinations/activities/notes (pgvector cosine),
    joined back to the activities they describe. This is the retrieval half of
    the agent. `outdoor_only` lets the caller bias toward indoor options when the
    weather is bad (pass False) or outdoor when it's good (pass True).

    Mirrors the weather app's _run_weather_search.
    """
    lakebase.ensure_place_embeddings_table()
    if not query or not query.strip():
        return {"status": "error", "message": "A search query is required."}
    top_k = max(1, min(int(top_k), 25))

    query_vec = embeddings.to_pgvector_literal(embeddings.embed_texts([query.strip()])[0])

    filters = ["e.source_type = 'activity'"]
    params: list = [query_vec]
    if trip_id:
        filters.append("e.trip_id = %s")
        params.append(trip_id)
    if outdoor_only is not None:
        filters.append("a.is_outdoor = %s")
        params.append(outdoor_only)
    where = " AND ".join(filters)
    params.append(query_vec)
    params.append(top_k)

    rows = lakebase.run_query(
        f"""
        SELECT a.id AS activity_id, a.name, a.category, a.is_outdoor, a.description,
               a.destination_id, d.name AS destination_name,
               1 - (e.embedding <=> %s::vector) AS similarity
        FROM {lakebase.PLACE_EMBEDDINGS_TABLE} e
        JOIN {lakebase.ACTIVITIES_TABLE} a ON a.id = e.source_id
        JOIN {lakebase.DESTINATIONS_TABLE} d ON d.id = a.destination_id
        WHERE {where}
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
        """,
        tuple(params),
    )
    # De-dup activities that matched on multiple chunks (keep best similarity).
    best: dict[str, dict] = {}
    for r in rows:
        aid = r["activity_id"]
        if aid not in best or r["similarity"] > best[aid]["similarity"]:
            best[aid] = r
    results = sorted(best.values(), key=lambda x: x["similarity"], reverse=True)
    return {"query": query, "count": len(results), "results": results}
