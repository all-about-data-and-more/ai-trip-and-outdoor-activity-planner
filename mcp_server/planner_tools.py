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
import logging
import os
import uuid
from datetime import date, datetime, timedelta

import embeddings
import lakebase
import llm
from clients import air_quality_client, geocoding_client, weather_client, wikimedia_client

# Reschedule / advisory thresholds (imperial defaults; overridable via env).
RAIN_PRECIP_CHANCE_PCT = float(os.environ.get("RAIN_PRECIP_CHANCE_PCT", "50"))
RAIN_PRECIP_AMOUNT = float(os.environ.get("RAIN_PRECIP_AMOUNT", "0.1"))
AQI_UNHEALTHY_THRESHOLD = float(os.environ.get("AQI_UNHEALTHY_THRESHOLD", "100"))

# Keyword heuristics for classifying an attraction as outdoor (drives
# weather-based rescheduling). Deliberately simple + explainable.
# For a WEATHER planner, "outdoor" means the visit happens largely outdoors
# (you walk grounds/paths). Japanese temples/shrines/castles/gardens are outdoor
# experiences, so they count as outdoor here even though they have buildings.
_OUTDOOR_HINTS = (
    "park", "garden", "gardens", "trail", "hike", "hiking", "mountain", "lake",
    "beach", "river", "riverside", "falls", "waterfall", "zoo", "botanical",
    "forest", "island", "bridge", "square", "plaza", "market", "harbor",
    "harbour", "bay", "cliff", "valley", "canyon", "vineyard", "outdoor",
    "monument", "castle", "ruins", "shrine", "temple", "pagoda", "gate",
    "tower", "palace", "promenade", "viewpoint", "arboretum",
)
_INDOOR_HINTS = (
    "museum", "gallery", "theatre", "theater", "aquarium", "library",
    "cathedral", "church", "mosque", "mall", "planetarium",
)


def _new_id() -> str:
    return uuid.uuid4().hex


def _guess_outdoor(name: str, text: str | None) -> bool:
    """Best-effort indoor/outdoor classification. The NAME is the strongest
    signal (a "Park"/"Garden"/"Shrine" is outdoor regardless of what its blurb
    mentions), so check it first; only fall back to the description when the name
    is ambiguous — otherwise a park whose blurb name-drops a nearby museum gets
    mis-tagged indoor."""
    n = (name or "").lower()
    if any(h in n for h in _OUTDOOR_HINTS):
        return True
    if any(h in n for h in _INDOOR_HINTS):
        return False
    blob = f"{n} {text or ''}".lower()
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


def update_trip(trip_id: str, title=None, start_date=None, end_date=None, notes=None) -> dict:
    """Update only the provided fields of a trip (WRITE). Returns the trip."""
    sets, params = [], []
    if title is not None:
        sets.append("title = %s"); params.append(title)
    if start_date is not None:
        sets.append("start_date = %s"); params.append(start_date or None)
    if end_date is not None:
        sets.append("end_date = %s"); params.append(end_date or None)
    if notes is not None:
        sets.append("notes = %s"); params.append(notes)
    if sets:
        params.append(trip_id)
        lakebase.run_write(
            f"UPDATE {lakebase.TRIPS_TABLE} SET {', '.join(sets)} WHERE id = %s", tuple(params))
    return get_trip(trip_id)


def delete_trip(trip_id: str) -> dict:
    """Delete a trip and ALL of its children — destinations, activities,
    itinerary items, packing items, weather snapshots, and embeddings (WRITE).
    Cascades manually since these tables aren't FK-cascade-linked, so nothing is
    left orphaned."""
    dest_ids = [r["id"] for r in lakebase.run_query(
        f"SELECT id FROM {lakebase.DESTINATIONS_TABLE} WHERE trip_id = %s", (trip_id,))]
    lakebase.run_write(f"DELETE FROM {lakebase.PLACE_EMBEDDINGS_TABLE} WHERE trip_id = %s", (trip_id,))
    if dest_ids:
        lakebase.run_write(
            f"DELETE FROM {lakebase.WEATHER_SNAPSHOTS_TABLE} WHERE destination_id = ANY(%s)", (dest_ids,))
        lakebase.run_write(
            f"DELETE FROM {lakebase.ACTIVITIES_TABLE} WHERE destination_id = ANY(%s)", (dest_ids,))
    lakebase.run_write(f"DELETE FROM {lakebase.ITINERARY_ITEMS_TABLE} WHERE trip_id = %s", (trip_id,))
    lakebase.run_write(f"DELETE FROM {lakebase.PACKING_ITEMS_TABLE} WHERE trip_id = %s", (trip_id,))
    lakebase.run_write(f"DELETE FROM {lakebase.DESTINATIONS_TABLE} WHERE trip_id = %s", (trip_id,))
    deleted = lakebase.run_write(f"DELETE FROM {lakebase.TRIPS_TABLE} WHERE id = %s", (trip_id,))
    return {"deleted": deleted, "trip_id": trip_id}


def delete_destination(destination_id: str) -> dict:
    """Delete a destination and its activities, weather snapshots, embeddings,
    and any itinerary items that reference it (WRITE)."""
    act_ids = [r["id"] for r in lakebase.run_query(
        f"SELECT id FROM {lakebase.ACTIVITIES_TABLE} WHERE destination_id = %s", (destination_id,))]
    lakebase.run_write(
        f"DELETE FROM {lakebase.PLACE_EMBEDDINGS_TABLE} "
        f"WHERE source_type = 'destination' AND source_id = %s", (destination_id,))
    if act_ids:
        lakebase.run_write(
            f"DELETE FROM {lakebase.PLACE_EMBEDDINGS_TABLE} "
            f"WHERE source_type = 'activity' AND source_id = ANY(%s)", (act_ids,))
    lakebase.run_write(
        f"DELETE FROM {lakebase.WEATHER_SNAPSHOTS_TABLE} WHERE destination_id = %s", (destination_id,))
    lakebase.run_write(
        f"DELETE FROM {lakebase.ITINERARY_ITEMS_TABLE} WHERE destination_id = %s", (destination_id,))
    lakebase.run_write(
        f"DELETE FROM {lakebase.ACTIVITIES_TABLE} WHERE destination_id = %s", (destination_id,))
    deleted = lakebase.run_write(
        f"DELETE FROM {lakebase.DESTINATIONS_TABLE} WHERE id = %s", (destination_id,))
    return {"deleted": deleted, "destination_id": destination_id}


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
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("planner").warning(
            "Wikimedia summary failed for %r (%s): %s", loc["name"], type(exc).__name__, exc)

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


# ===========================================================================
# Phase 2 — itinerary, rescheduling, packing, edits, and explanations.
# These are the "generate / reschedule / build / edit / explain" agent
# capabilities the capstone requires. Heavy logic is DETERMINISTIC; the LLM is
# used only to phrase the explanation.
# ===========================================================================

# Time slots itinerary items are dropped into, in order.
_SLOTS = [("09:00", "11:00"), ("11:30", "13:30"), ("14:00", "16:00"), ("16:30", "18:30")]


def _as_date(value):
    """Coerce a value (date, or 'YYYY-MM-DD' string) to a date, or None."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _trip_days(trip: dict) -> list[date]:
    """Inclusive list of dates for the trip. Falls back to a 3-day window from
    today when the trip has no start/end date set."""
    start = _as_date(trip.get("start_date"))
    end = _as_date(trip.get("end_date"))
    if not start:
        start = date.today()
        end = start + timedelta(days=2)
    if not end or end < start:
        end = start
    days, d = [], start
    while d <= end and len(days) < 30:  # cap to keep it sane
        days.append(d)
        d += timedelta(days=1)
    return days


def _is_bad_weather(snap: dict | None) -> tuple[bool, str]:
    """Decide whether a day's conditions are unsuitable for outdoor activity,
    with a plain-language reason. Thresholds are the RAIN_*/AQI_* env knobs."""
    if not snap:
        return False, ""
    chance = snap.get("precip_chance_pct") or 0
    amount = snap.get("precip_amount") or 0
    aqi = snap.get("aqi") or 0
    reasons = []
    if chance >= RAIN_PRECIP_CHANCE_PCT or amount >= RAIN_PRECIP_AMOUNT:
        reasons.append(f"{int(chance)}% chance of rain")
    if aqi >= AQI_UNHEALTHY_THRESHOLD:
        reasons.append(f"unhealthy air (AQI {int(aqi)})")
    return (len(reasons) > 0), " and ".join(reasons)


def _get_snapshot(destination_id: str, day: date) -> dict | None:
    rows = lakebase.run_query(
        f"SELECT * FROM {lakebase.WEATHER_SNAPSHOTS_TABLE} "
        f"WHERE destination_id = %s AND snapshot_date = %s",
        (destination_id, day),
    )
    return rows[0] if rows else None


def _assign_days_to_destinations(days: list[date], dests: list[dict]) -> list[dict]:
    """Map each trip day to a destination. Honors arrival/depart windows when
    set; otherwise spreads destinations evenly across the days."""
    assigned = []
    for i, day in enumerate(days):
        match = None
        for d in dests:
            a, dep = _as_date(d.get("arrival_date")), _as_date(d.get("depart_date"))
            if a and dep and a <= day <= dep:
                match = d
                break
            if a and not dep and a == day:
                match = d
                break
        assigned.append(match or dests[i % len(dests)])
    return assigned


def list_itinerary(trip_id: str) -> list[dict]:
    return lakebase.run_query(
        f"SELECT * FROM {lakebase.ITINERARY_ITEMS_TABLE} WHERE trip_id = %s "
        f"ORDER BY day_date NULLS LAST, sort_order, start_time",
        (trip_id,),
    )


def list_packing(trip_id: str) -> list[dict]:
    return lakebase.run_query(
        f"SELECT * FROM {lakebase.PACKING_ITEMS_TABLE} WHERE trip_id = %s "
        f"ORDER BY category, item",
        (trip_id,),
    )


def set_packing_packed(item_id: str, packed: bool) -> dict:
    """Check/uncheck a packing item (WRITE)."""
    updated = lakebase.run_write(
        f"UPDATE {lakebase.PACKING_ITEMS_TABLE} SET packed = %s WHERE id = %s",
        (bool(packed), item_id),
    )
    return {"updated": updated, "id": item_id, "packed": bool(packed)}


def generate_itinerary(trip_id: str, items_per_day: int = 3) -> dict:
    """
    Build a weather-aware day-by-day itinerary (WRITE). For each trip day it
    picks a destination, pulls that day's forecast/AQI into weather_snapshots,
    and schedules activities — indoor-first on bad-weather days, outdoor-first on
    good ones. Regenerates from scratch each call (clears prior planned items).
    """
    lakebase.ensure_itinerary_items_table()
    lakebase.ensure_weather_snapshots_table()
    trip = get_trip(trip_id)
    if trip.get("status") == "error":
        return trip
    dests = trip.get("destinations") or []
    if not dests:
        return {"status": "error", "message": "Add at least one destination first."}

    days = _trip_days(trip)
    for d in dests:  # populate weather so scheduling is weather-aware
        snapshot_weather(d["id"], days=len(days) + 1)

    day_dest = _assign_days_to_destinations(days, dests)
    lakebase.run_write(
        f"DELETE FROM {lakebase.ITINERARY_ITEMS_TABLE} WHERE trip_id = %s AND status = 'planned'",
        (trip_id,),
    )

    created = 0
    for day, dest in zip(days, day_dest):
        snap = _get_snapshot(dest["id"], day)
        bad, reason = _is_bad_weather(snap)
        acts = list_activities(dest["id"])
        outdoor = [a for a in acts if a["is_outdoor"]]
        indoor = [a for a in acts if not a["is_outdoor"]]
        ordered = (indoor + outdoor) if bad else (outdoor + indoor)
        chosen = ordered[:items_per_day] or [None]

        for j, act in enumerate(chosen):
            slot = _SLOTS[j % len(_SLOTS)]
            if act is None:
                title, is_outdoor, act_id = f"Explore {dest['name']}", False, None
                note = ""
            else:
                title, is_outdoor, act_id = act["name"], act["is_outdoor"], act["id"]
                note = ""
                if bad and is_outdoor:
                    note = f"Weather watch: {reason} expected — may be moved by reschedule."
                elif bad:
                    note = f"Indoor pick for likely {reason}."
            lakebase.run_write(
                f"""
                INSERT INTO {lakebase.ITINERARY_ITEMS_TABLE}
                    (id, trip_id, destination_id, activity_id, title, day_date,
                     start_time, end_time, is_outdoor, status, notes, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'planned', %s, %s)
                """,
                (_new_id(), trip_id, dest["id"], act_id, title, day,
                 slot[0], slot[1], is_outdoor, note, j),
            )
            created += 1

    return {
        "trip_id": trip_id,
        "days": len(days),
        "items_created": created,
        "itinerary": list_itinerary(trip_id),
    }


def _find_good_day(destination_id: str, current_day: date, days: list[date]) -> date | None:
    """Nearest trip day (to current_day) whose weather is NOT bad, for the same
    destination. Returns None if no clearer day exists."""
    candidates = []
    for day in days:
        if day == current_day:
            continue
        ok, _ = _is_bad_weather(_get_snapshot(destination_id, day))
        if not ok:  # ok==False means NOT bad -> good
            candidates.append(day)
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - current_day).days))


def reschedule_for_weather(trip_id: str) -> dict:
    """
    Move outdoor itinerary items off bad-weather days (WRITE). Refreshes each
    destination's forecast/AQI, then for every outdoor item on a rainy / poor-AQI
    day, moves it to the nearest clear day for that destination (or flags it if
    none exists), recording the reason on the item. Returns the changes made —
    this is the data explain_changes narrates.
    """
    trip = get_trip(trip_id)
    if trip.get("status") == "error":
        return trip
    dests = trip.get("destinations") or []
    days = _trip_days(trip)
    for d in dests:
        snapshot_weather(d["id"], days=len(days) + 1)

    changes = []
    for item in list_itinerary(trip_id):
        if not item["is_outdoor"] or item["status"] == "rescheduled":
            continue
        day = _as_date(item.get("day_date"))
        if not day or not item.get("destination_id"):
            continue
        bad, reason = _is_bad_weather(_get_snapshot(item["destination_id"], day))
        if not bad:
            continue

        better = _find_good_day(item["destination_id"], day, days)
        if better:
            note = f"Moved from {day.isoformat()} to {better.isoformat()} — {reason} forecast."
            lakebase.run_write(
                f"UPDATE {lakebase.ITINERARY_ITEMS_TABLE} SET day_date = %s, status = 'rescheduled', "
                f"notes = %s, updated_at = now() WHERE id = %s",
                (better, note, item["id"]),
            )
            changes.append({"item": item["title"], "from": day.isoformat(),
                            "to": better.isoformat(), "reason": reason})
        else:
            note = (f"Flagged: {reason} on {day.isoformat()} and no clearer day is available — "
                    f"consider an indoor alternative.")
            lakebase.run_write(
                f"UPDATE {lakebase.ITINERARY_ITEMS_TABLE} SET status = 'rescheduled', notes = %s, "
                f"updated_at = now() WHERE id = %s",
                (note, item["id"]),
            )
            changes.append({"item": item["title"], "from": day.isoformat(),
                            "to": None, "reason": reason})

    return {"trip_id": trip_id, "changes_made": len(changes), "changes": changes}


def build_packing_list(trip_id: str) -> dict:
    """
    Generate a weather- and activity-aware packing list (WRITE). Aggregates the
    trip's forecast/AQI + activities into concrete items with reasons, writing
    them to packing_items. Regenerates from scratch each call.
    """
    lakebase.ensure_packing_items_table()
    trip = get_trip(trip_id)
    if trip.get("status") == "error":
        return trip
    dests = trip.get("destinations") or []
    days = _trip_days(trip)
    for d in dests:
        snapshot_weather(d["id"], days=len(days) + 1)

    snaps = []
    for d in dests:
        snaps += lakebase.run_query(
            f"SELECT * FROM {lakebase.WEATHER_SNAPSHOTS_TABLE} WHERE destination_id = %s",
            (d["id"],),
        )
    lows = [s["temp_low"] for s in snaps if s.get("temp_low") is not None]
    highs = [s["temp_high"] for s in snaps if s.get("temp_high") is not None]
    uvs = [s["uv_index_max"] for s in snaps if s.get("uv_index_max") is not None]
    min_low = min(lows) if lows else None
    max_high = max(highs) if highs else None
    max_uv = max(uvs) if uvs else None
    any_rain = any((_is_bad_weather(s)[0] and (s.get("precip_chance_pct") or 0) >= RAIN_PRECIP_CHANCE_PCT) for s in snaps)
    any_bad_air = any((s.get("aqi") or 0) >= AQI_UNHEALTHY_THRESHOLD for s in snaps)
    any_outdoor = any(
        a["is_outdoor"] for d in dests for a in list_activities(d["id"])
    )

    items: list[tuple[str, str, str]] = [  # (category, item, reason)
        ("documents", "Passport / ID", "Essential for travel"),
        ("documents", "Travel insurance & bookings", "Essential for travel"),
        ("electronics", "Phone + charger", "Everyday essential"),
        ("electronics", "Power adapter", "Different outlets abroad"),
        ("health", "Personal medications", "Everyday essential"),
        ("essentials", "Reusable water bottle", "Stay hydrated between activities"),
    ]
    if min_low is not None and min_low < 50:
        items.append(("clothing", "Warm jacket / layers", f"Lows near {int(min_low)}° expected"))
    if max_high is not None and max_high > 80:
        items.append(("clothing", "Light, breathable clothing", f"Highs near {int(max_high)}° expected"))
        items.append(("accessories", "Sun hat", "Hot, sunny days expected"))
    if any_rain:
        items.append(("gear", "Compact umbrella", "Rain in the forecast"))
        items.append(("clothing", "Waterproof jacket", "Rain in the forecast"))
    if max_uv is not None and max_uv >= 6:
        items.append(("health", "Sunscreen (SPF 30+)", f"High UV index (~{int(max_uv)})"))
        items.append(("accessories", "Sunglasses", "High UV index"))
    if any_bad_air:
        items.append(("health", "N95 mask", "Poor air-quality days forecast"))
    if any_outdoor:
        items.append(("gear", "Comfortable walking / hiking shoes", "Outdoor activities planned"))
        items.append(("gear", "Small daypack", "Outdoor activities planned"))

    lakebase.run_write(f"DELETE FROM {lakebase.PACKING_ITEMS_TABLE} WHERE trip_id = %s", (trip_id,))
    for category, item, reason in items:
        lakebase.run_write(
            f"INSERT INTO {lakebase.PACKING_ITEMS_TABLE} (id, trip_id, category, item, reason) "
            f"VALUES (%s, %s, %s, %s, %s)",
            (_new_id(), trip_id, category, item, reason),
        )

    return {"trip_id": trip_id, "items_created": len(items), "packing": list_packing(trip_id)}


def add_itinerary_item(trip_id: str, title: str, day_date=None, destination_id=None,
                       activity_id=None, is_outdoor: bool = False, start_time=None,
                       end_time=None, notes=None) -> dict:
    """Add a single itinerary item (WRITE)."""
    lakebase.ensure_itinerary_items_table()
    item_id = _new_id()
    lakebase.run_write(
        f"""
        INSERT INTO {lakebase.ITINERARY_ITEMS_TABLE}
            (id, trip_id, destination_id, activity_id, title, day_date, start_time,
             end_time, is_outdoor, status, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'planned', %s)
        """,
        (item_id, trip_id, destination_id, activity_id, title, _as_date(day_date) or None,
         start_time, end_time, is_outdoor, notes),
    )
    return {"id": item_id, "trip_id": trip_id, "title": title}


def remove_itinerary_item(item_id: str) -> dict:
    """Remove an itinerary item (WRITE)."""
    deleted = lakebase.run_write(
        f"DELETE FROM {lakebase.ITINERARY_ITEMS_TABLE} WHERE id = %s", (item_id,)
    )
    return {"removed": deleted, "id": item_id}


def move_itinerary_item(item_id: str, day_date=None, start_time=None,
                        end_time=None, sort_order=None) -> dict:
    """Move/reschedule an itinerary item to a new day or time (WRITE)."""
    sets, params = [], []
    if day_date is not None:
        sets.append("day_date = %s")
        params.append(_as_date(day_date))
    if start_time is not None:
        sets.append("start_time = %s")
        params.append(start_time)
    if end_time is not None:
        sets.append("end_time = %s")
        params.append(end_time)
    if sort_order is not None:
        sets.append("sort_order = %s")
        params.append(int(sort_order))
    if not sets:
        return {"status": "error", "message": "Nothing to update."}
    sets.append("updated_at = now()")
    params.append(item_id)
    updated = lakebase.run_write(
        f"UPDATE {lakebase.ITINERARY_ITEMS_TABLE} SET {', '.join(sets)} WHERE id = %s",
        tuple(params),
    )
    return {"updated": updated, "id": item_id}


def explain_changes(trip_id: str) -> dict:
    """
    Explain, in plain language, the weather-based changes the agent made — the
    "explain why it made each change" capability. Reads rescheduled items +
    their reasons and asks the Foundation Model to summarize; falls back to a
    deterministic summary if the model is unavailable.
    """
    rescheduled = [i for i in list_itinerary(trip_id) if i["status"] == "rescheduled"]
    if not rescheduled:
        return {"trip_id": trip_id, "changes": [],
                "explanation": "No weather-based changes were needed — every activity is on a suitable day."}

    lines = [f"- {i['title']}: {i.get('notes') or 'rescheduled'}" for i in rescheduled]
    deterministic = "I adjusted your plan for the weather:\n" + "\n".join(lines)

    text = llm.simple_chat(
        system=("You are a concise travel-planning assistant. Explain, warmly and briefly, why "
                "these outdoor activities were rescheduled based on weather/air quality. Use only "
                "the facts given."),
        user="Here are the changes I made:\n" + "\n".join(lines),
    )
    return {
        "trip_id": trip_id,
        "changes": [{"item": i["title"], "notes": i.get("notes")} for i in rescheduled],
        "explanation": text or deterministic,
    }
