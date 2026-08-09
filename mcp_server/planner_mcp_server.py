"""
Trip Planner MCP server.

Exposes the planner's tools over MCP (Model Context Protocol) so a Databricks
Agent Bricks agent can call them like any other tool — the "Option 2" agent
surface. Every tool is a thin @mcp.tool wrapper over planner_tools.py (the SAME
shared tool layer the in-app chat agent uses), so the two agent surfaces never
diverge. Tools catch errors and return a clean {"status": "error", ...} dict
instead of raising, so a bad id or an API outage never reaches the agent as a
stack trace — the weather-mcp-server / Day-3 pattern.

The agent both RETRIEVES (search_activities, get_forecast, get_air_quality,
list_itinerary) and WRITES (add_destination, generate_itinerary,
reschedule_for_weather, build_packing_list, add/remove/move_itinerary_item) —
the capstone's required "searches AND writes" agent.

Deploy this folder as its own Databricks App (app.yaml runs this file over
streamable HTTP), then register its /mcp URL as an external MCP server for your
Agent Bricks agent. See agent/agent_setup.md.

Run locally:
    python planner_mcp_server.py
"""

import logging
import os

from fastmcp import FastMCP

import planner_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("planner-mcp-server")

mcp = FastMCP("trip-planner")


def _safe(fn, *args, **kwargs):
    """Call a planner_tools function, converting exceptions into a clean error
    dict so the agent never sees a raw traceback."""
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.exception("%s failed", getattr(fn, "__name__", "tool"))
        return {"status": "error", "message": f"{getattr(fn, '__name__', 'tool')} failed: {e}"}


# --- Trips & destinations --------------------------------------------------
@mcp.tool
def list_trips(user_email: str) -> list:
    """List a user's trips. `user_email` identifies the traveler."""
    return _safe(planner_tools.list_trips, user_email)


@mcp.tool
def get_trip(trip_id: str) -> dict:
    """Get one trip with its destinations."""
    return _safe(planner_tools.get_trip, trip_id)


@mcp.tool
def add_destination(trip_id: str, name: str) -> dict:
    """Add a destination to a trip. Fetches a Wikimedia description and seeds
    nearby attractions as activities. `name` is a place like 'Kyoto, Japan'."""
    return _safe(planner_tools.add_destination, trip_id, name)


# --- Weather & air quality (retrieval) -------------------------------------
@mcp.tool
def get_forecast(location: str, days: int = 7) -> dict:
    """Daily weather forecast for a place name or 'lat,lon' (Open-Meteo)."""
    return _safe(planner_tools.get_forecast, location, days)


@mcp.tool
def get_air_quality(location: str, days: int = 5) -> dict:
    """Per-day AQI / PM / UV / pollen for a place name or 'lat,lon' (Open-Meteo)."""
    return _safe(planner_tools.get_air_quality, location, days)


# --- Context-engineering retrieval -----------------------------------------
@mcp.tool
def search_activities(query: str, trip_id: str = None, outdoor_only: bool = None) -> dict:
    """Semantically search suitable activities (pgvector). Optionally scope to a
    trip and bias toward outdoor (True) or indoor (False) options."""
    return _safe(planner_tools.search_activities, query, trip_id=trip_id, outdoor_only=outdoor_only)


@mcp.tool
def refresh_embeddings() -> dict:
    """Embed any new destinations/activities/notes into the vector store so
    search_activities can retrieve them."""
    import embedding_job
    return _safe(embedding_job.run)


# --- Itinerary, rescheduling, packing (writes) -----------------------------
@mcp.tool
def generate_itinerary(trip_id: str, items_per_day: int = 3) -> dict:
    """Build/regenerate a weather-aware day-by-day itinerary for a trip."""
    return _safe(planner_tools.generate_itinerary, trip_id, items_per_day)


@mcp.tool
def reschedule_for_weather(trip_id: str) -> dict:
    """Move outdoor activities off rainy or poor-air-quality days, recording why."""
    return _safe(planner_tools.reschedule_for_weather, trip_id)


@mcp.tool
def build_packing_list(trip_id: str) -> dict:
    """Generate a weather- and activity-aware packing list for a trip."""
    return _safe(planner_tools.build_packing_list, trip_id)


@mcp.tool
def list_itinerary(trip_id: str) -> list:
    """List a trip's itinerary items."""
    return _safe(planner_tools.list_itinerary, trip_id)


@mcp.tool
def add_itinerary_item(trip_id: str, title: str, day_date: str = None,
                       is_outdoor: bool = False) -> dict:
    """Add a single itinerary item. `day_date` is 'YYYY-MM-DD'."""
    return _safe(planner_tools.add_itinerary_item, trip_id, title,
                 day_date=day_date, is_outdoor=is_outdoor)


@mcp.tool
def remove_itinerary_item(item_id: str) -> dict:
    """Remove an itinerary item by id."""
    return _safe(planner_tools.remove_itinerary_item, item_id)


@mcp.tool
def move_itinerary_item(item_id: str, day_date: str = None) -> dict:
    """Move an itinerary item to a new day ('YYYY-MM-DD')."""
    return _safe(planner_tools.move_itinerary_item, item_id, day_date=day_date)


@mcp.tool
def explain_changes(trip_id: str) -> dict:
    """Explain, in plain language, the weather-based changes made to the itinerary."""
    return _safe(planner_tools.explain_changes, trip_id)


if __name__ == "__main__":
    # Databricks Apps route external HTTP traffic to this port; streamable-http
    # is the transport Databricks' MCP client/gateway expects (Day-3 pattern).
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", "8000")))
    mcp.run(transport="http", host="0.0.0.0", port=port)
