"""
In-app planner agent — a Foundation Model tool-calling loop over planner_tools.

This is the "Option 1" surface: the app's chat panel posts here, and the agent
decides which planner tools to call (retrieval AND writes) to answer. It shares
the exact same tool layer (planner_tools.py) as the FastMCP server, so the two
agent surfaces can never diverge.

Robustness: if the workspace's model endpoint doesn't support tool-calling (or
is unavailable) on Free Edition, `llm.tool_chat` returns None and we fall back
to a deterministic intent router that maps the message to the right planner
tool. So the agent always does something sensible — never a hard failure.
"""

import json
import logging

import llm
import planner_tools

logger = logging.getLogger("agent")

MAX_TOOL_TURNS = 5

_SYSTEM = """You are the AI Trip & Outdoor Activity Planner agent.

You are helping the user with THIS trip: {trip_summary}

Capabilities (via tools): generate a weather-aware day-by-day itinerary,
reschedule outdoor activities when rain or poor air quality is forecast, build a
packing list, add/remove/move itinerary items, search suitable activities, and
look up weather / air quality. When you make weather-based changes, briefly
explain why. Always call a tool before stating itinerary/weather facts — never
invent forecasts. Keep replies concise and practical. The current trip is
already selected, so trip-scoped tools act on it automatically."""


def _tool_specs() -> list[dict]:
    """OpenAI-format tool schemas for the trip-scoped agent. Trip-scoped tools
    take no trip_id (it's injected from context)."""
    def t(name, desc, props=None, required=None):
        return {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props or {},
                           "required": required or []}}}
    return [
        t("generate_itinerary", "Build/regenerate a weather-aware day-by-day itinerary for the trip."),
        t("reschedule_for_weather", "Move outdoor activities off rainy or poor-air-quality days."),
        t("build_packing_list", "Generate a weather- and activity-aware packing list."),
        t("explain_changes", "Explain the weather-based changes that were made to the itinerary."),
        t("list_itinerary", "List the current itinerary items."),
        t("search_activities", "Semantically search suitable activities for the trip.",
          {"query": {"type": "string"}, "outdoor_only": {"type": "boolean"}}, ["query"]),
        t("add_destination", "Add a destination to the trip (fetches description + attractions).",
          {"name": {"type": "string", "description": "e.g. 'Kyoto, Japan'"}}, ["name"]),
        t("get_forecast", "Get a daily weather forecast for a place.",
          {"location": {"type": "string"}, "days": {"type": "integer"}}, ["location"]),
        t("get_air_quality", "Get AQI / PM / UV / pollen for a place.",
          {"location": {"type": "string"}, "days": {"type": "integer"}}, ["location"]),
        t("add_itinerary_item", "Add one itinerary item.",
          {"title": {"type": "string"}, "day_date": {"type": "string", "description": "YYYY-MM-DD"},
           "is_outdoor": {"type": "boolean"}}, ["title"]),
        t("remove_itinerary_item", "Remove an itinerary item by id.",
          {"item_id": {"type": "string"}}, ["item_id"]),
        t("move_itinerary_item", "Move an itinerary item to a new day.",
          {"item_id": {"type": "string"}, "day_date": {"type": "string"}}, ["item_id"]),
    ]


def _dispatch(trip_id: str) -> dict:
    """Map tool name -> callable(args_dict). Trip-scoped tools bind trip_id."""
    pt = planner_tools
    return {
        "generate_itinerary": lambda a: pt.generate_itinerary(trip_id),
        "reschedule_for_weather": lambda a: pt.reschedule_for_weather(trip_id),
        "build_packing_list": lambda a: pt.build_packing_list(trip_id),
        "explain_changes": lambda a: pt.explain_changes(trip_id),
        "list_itinerary": lambda a: pt.list_itinerary(trip_id),
        "search_activities": lambda a: pt.search_activities(
            a.get("query", ""), trip_id=trip_id, outdoor_only=a.get("outdoor_only")),
        "add_destination": lambda a: pt.add_destination(trip_id, a.get("name", "")),
        "get_forecast": lambda a: pt.get_forecast(a.get("location", ""), a.get("days", 7)),
        "get_air_quality": lambda a: pt.get_air_quality(a.get("location", ""), a.get("days", 5)),
        "add_itinerary_item": lambda a: pt.add_itinerary_item(
            trip_id, a.get("title", ""), day_date=a.get("day_date"),
            is_outdoor=a.get("is_outdoor", False)),
        "remove_itinerary_item": lambda a: pt.remove_itinerary_item(a.get("item_id", "")),
        "move_itinerary_item": lambda a: pt.move_itinerary_item(
            a.get("item_id", ""), day_date=a.get("day_date")),
    }


def _trim(result, max_items: int = 8):
    """Shrink big tool results before feeding them back into the model context."""
    if isinstance(result, dict):
        out = {}
        for k, v in result.items():
            out[k] = _trim(v, max_items) if isinstance(v, (list, dict)) else v
        return out
    if isinstance(result, list):
        return result[:max_items]
    return result


def _trip_summary(trip_id: str) -> str:
    trip = planner_tools.get_trip(trip_id)
    if trip.get("status") == "error":
        return f"(trip {trip_id})"
    dests = ", ".join(d["name"] for d in trip.get("destinations", [])) or "no destinations yet"
    return (f"'{trip.get('title')}' ({trip.get('start_date') or '?'} to "
            f"{trip.get('end_date') or '?'}); destinations: {dests}")


def chat(user_message: str, trip_id: str, history: list[dict] | None = None) -> dict:
    """Run one agent turn. Returns {reply, tool_calls, fallback}."""
    if not trip_id:
        return {"reply": "Select or create a trip first, then I can plan it.",
                "tool_calls": [], "fallback": True}

    tools = _tool_specs()
    dispatch = _dispatch(trip_id)
    messages = [{"role": "system", "content": _SYSTEM.format(trip_summary=_trip_summary(trip_id))}]
    messages += (history or [])
    messages.append({"role": "user", "content": user_message})

    made: list[dict] = []
    for _ in range(MAX_TOOL_TURNS):
        msg = llm.tool_chat(messages, tools)
        if msg is None:  # tool-calling unavailable -> deterministic fallback
            return _fallback(user_message, trip_id, dispatch)

        tool_calls = getattr(msg, "tool_calls", None)
        try:
            messages.append(msg.model_dump(exclude_unset=True))
        except Exception:  # noqa: BLE001 - be tolerant of client shape
            messages.append({"role": "assistant", "content": msg.content or ""})

        if not tool_calls:
            return {"reply": msg.content or "", "tool_calls": made, "fallback": False}

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            fn = dispatch.get(name)
            try:
                result = fn(args) if fn else {"status": "error", "message": f"unknown tool {name}"}
            except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
                result = {"status": "error", "message": str(exc)}
            made.append({"tool": name, "args": args})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(_trim(result), default=str)})

    return {"reply": "I made several updates — check your itinerary and packing list.",
            "tool_calls": made, "fallback": False}


# ---------------------------------------------------------------------------
# Deterministic fallback (used when tool-calling isn't available on the endpoint)
# ---------------------------------------------------------------------------
def _fallback(user_message: str, trip_id: str, dispatch: dict) -> dict:
    """Keyword-route the message to a single planner tool and summarize."""
    m = user_message.lower()
    made = []

    def run(tool, summary):
        made.append({"tool": tool, "args": {}})
        return dispatch[tool]({}), summary

    if any(w in m for w in ("pack", "bring", "luggage")):
        res, _ = run("build_packing_list", None)
        reply = f"I built a packing list with {res.get('items_created', 0)} items based on the forecast and your activities."
    elif any(w in m for w in ("reschedul", "rain", "weather", "air quality", "aqi")):
        res, _ = run("reschedule_for_weather", None)
        n = res.get("changes_made", 0)
        reply = (f"I rescheduled {n} outdoor activit{'y' if n == 1 else 'ies'} off bad-weather days."
                 if n else "I checked the weather — no outdoor activities needed moving.")
    elif any(w in m for w in ("explain", "why", "reason")):
        res, _ = run("explain_changes", None)
        reply = res.get("explanation", "")
    elif any(w in m for w in ("itinerary", "plan", "schedule", "day by day", "day-by-day")):
        res, _ = run("generate_itinerary", None)
        reply = f"I generated a {res.get('days', 0)}-day itinerary with {res.get('items_created', 0)} activities, weather-aware."
    elif any(w in m for w in ("find", "search", "recommend", "activit", "things to do", "suggest")):
        made.append({"tool": "search_activities", "args": {"query": user_message}})
        res = planner_tools.search_activities(user_message, trip_id=trip_id)
        names = ", ".join(r["name"] for r in res.get("results", [])[:5]) or "nothing yet — add destinations and refresh embeddings"
        reply = f"Top matches: {names}."
    else:
        reply = ("I can build your itinerary, reschedule for weather, make a packing list, "
                 "search activities, or explain my changes. What would you like?")

    return {"reply": reply, "tool_calls": made, "fallback": True}
