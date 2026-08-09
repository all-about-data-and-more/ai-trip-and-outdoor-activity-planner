"""
AI Trip & Outdoor Activity Planner — Databricks App (Flask frontend + REST API).

Users save destinations and preferences; the agent builds a weather- and
air-quality-aware itinerary, reschedules outdoor activities on bad conditions,
builds a packing list, and explains its choices.

This module is the human-facing surface: a single-page UI (templates/index.html)
plus a small JSON API. All real work lives in planner_tools.py (the shared tool
layer the agent also uses), so these routes stay thin — the same pattern as the
weather app's app.py.

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import logging
import os

from flask import Flask, jsonify, render_template, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import agent
import embeddings
import embedding_job
import lakebase
import planner_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trip-planner")

app = Flask(__name__)

# Ensure the full schema exists once at startup (idempotent), and warm the
# embedding model so the first /search doesn't pay model-load latency.
try:
    lakebase.ensure_all_tables()
except Exception:  # noqa: BLE001 - don't crash startup if Lakebase is briefly unreachable
    logger.exception("ensure_all_tables() failed at startup; will retry per-request")
embeddings.get_model()


def _current_user_email() -> str:
    """Resolve the logged-in user. Databricks Apps inject X-Forwarded-Email;
    fall back to the SDK's current_user for local dev, then a dev default."""
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    try:
        from databricks.sdk import WorkspaceClient
        return WorkspaceClient().current_user.me().user_name
    except Exception:  # noqa: BLE001 - local dev without a Databricks profile
        return os.environ.get("DEV_USER_EMAIL", "dev@example.com")


@app.errorhandler(Exception)
def handle_exception(err):
    """Return JSON for every unhandled error so the frontend's resp.json()
    never chokes on an HTML error page (ported from the weather app)."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------
@app.route("/trips", methods=["GET"])
def get_trips():
    return jsonify(planner_tools.list_trips(_current_user_email()))


@app.route("/trips", methods=["POST"])
def post_trip():
    body = request.json if request.is_json else {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "'title' is required"}), 400
    trip = planner_tools.create_trip(
        _current_user_email(), title,
        start_date=body.get("start_date"), end_date=body.get("end_date"),
        notes=body.get("notes"),
    )
    return jsonify(trip), 201


@app.route("/trips/<trip_id>", methods=["GET"])
def get_one_trip(trip_id):
    trip = planner_tools.get_trip(trip_id)
    if trip.get("status") == "error":
        return jsonify(trip), 404
    return jsonify(trip)


@app.route("/trips/<trip_id>", methods=["PATCH"])
def patch_trip(trip_id):
    body = request.json if request.is_json else {}
    if "title" in body and not (body.get("title") or "").strip():
        return jsonify({"error": "'title' cannot be empty"}), 400
    trip = planner_tools.update_trip(
        trip_id, title=body.get("title"), start_date=body.get("start_date"),
        end_date=body.get("end_date"), notes=body.get("notes"),
    )
    if trip.get("status") == "error":
        return jsonify(trip), 404
    return jsonify(trip)


@app.route("/trips/<trip_id>", methods=["DELETE"])
def delete_one_trip(trip_id):
    return jsonify(planner_tools.delete_trip(trip_id))


# ---------------------------------------------------------------------------
# Destinations & activities
# ---------------------------------------------------------------------------
@app.route("/trips/<trip_id>/destinations", methods=["GET"])
def get_destinations(trip_id):
    return jsonify(planner_tools.list_destinations(trip_id))


@app.route("/trips/<trip_id>/destinations", methods=["POST"])
def post_destination(trip_id):
    body = request.json if request.is_json else {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "'name' is required"}), 400
    try:
        dest = planner_tools.add_destination(
            trip_id, name,
            arrival_date=body.get("arrival_date"), depart_date=body.get("depart_date"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(dest), 201


@app.route("/destinations/<destination_id>/activities", methods=["GET"])
def get_activities(destination_id):
    return jsonify(planner_tools.list_activities(destination_id))


@app.route("/destinations/<destination_id>", methods=["DELETE"])
def delete_destination_route(destination_id):
    return jsonify(planner_tools.delete_destination(destination_id))


@app.route("/destinations/<destination_id>/weather", methods=["POST"])
def post_destination_weather(destination_id):
    """Fetch forecast + air quality and upsert weather_snapshots for a destination."""
    body = request.json if request.is_json else {}
    days = int(body.get("days", 7))
    result = planner_tools.snapshot_weather(destination_id, days)
    status = 404 if result.get("status") == "error" else 200
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Raw weather / air-quality reads (no persistence) — handy for the UI + agent
# ---------------------------------------------------------------------------
@app.route("/weather", methods=["GET"])
def weather_read():
    location = request.args.get("location", "").strip()
    if not location:
        return jsonify({"error": "'location' query param is required"}), 400
    return jsonify(planner_tools.get_forecast(location, int(request.args.get("days", 7))))


@app.route("/air", methods=["GET"])
def air_read():
    location = request.args.get("location", "").strip()
    if not location:
        return jsonify({"error": "'location' query param is required"}), 400
    return jsonify(planner_tools.get_air_quality(location, int(request.args.get("days", 5))))


# ---------------------------------------------------------------------------
# Context engineering: embeddings refresh + retrieval
# ---------------------------------------------------------------------------
@app.route("/embeddings/refresh", methods=["POST"])
def embeddings_refresh():
    """Chunk + embed any destinations/activities/notes that don't have vectors
    yet, into place_embeddings. This is the Serverless-safe serving path (runs
    in App compute); the Spark notebook is the batch equivalent."""
    body = request.json if request.is_json else {}
    result = embedding_job.run(
        chunk_size=int(body.get("chunk_size", embedding_job.DEFAULT_CHUNK_SIZE)),
        chunk_overlap=int(body.get("chunk_overlap", embedding_job.DEFAULT_CHUNK_OVERLAP)),
    )
    return jsonify(result)


@app.route("/search", methods=["POST"])
def search():
    """Semantic search over embedded activities (pgvector cosine)."""
    body = request.json if request.is_json else {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "'query' is required"}), 400
    result = planner_tools.search_activities(
        query, trip_id=body.get("trip_id"),
        top_k=int(body.get("top_k", 8)), outdoor_only=body.get("outdoor_only"),
    )
    return jsonify(result)


# ---------------------------------------------------------------------------
# Itinerary, rescheduling, packing, explanations (the agent's write tools)
# ---------------------------------------------------------------------------
@app.route("/trips/<trip_id>/itinerary", methods=["GET"])
def get_itinerary(trip_id):
    return jsonify(planner_tools.list_itinerary(trip_id))


@app.route("/trips/<trip_id>/itinerary/generate", methods=["POST"])
def post_generate_itinerary(trip_id):
    body = request.json if request.is_json else {}
    result = planner_tools.generate_itinerary(trip_id, items_per_day=int(body.get("items_per_day", 3)))
    status = 400 if result.get("status") == "error" else 200
    return jsonify(result), status


@app.route("/trips/<trip_id>/reschedule", methods=["POST"])
def post_reschedule(trip_id):
    return jsonify(planner_tools.reschedule_for_weather(trip_id))


@app.route("/trips/<trip_id>/packing", methods=["GET"])
def get_packing(trip_id):
    return jsonify(planner_tools.list_packing(trip_id))


@app.route("/trips/<trip_id>/packing", methods=["POST"])
def post_packing(trip_id):
    return jsonify(planner_tools.build_packing_list(trip_id))


@app.route("/trips/<trip_id>/explain", methods=["GET"])
def get_explain(trip_id):
    return jsonify(planner_tools.explain_changes(trip_id))


@app.route("/packing/items/<item_id>/toggle", methods=["POST"])
def toggle_packing_item(item_id):
    body = request.json if request.is_json else {}
    return jsonify(planner_tools.set_packing_packed(item_id, bool(body.get("packed", True))))


@app.route("/trips/<trip_id>/itinerary/items", methods=["POST"])
def post_itinerary_item(trip_id):
    body = request.json if request.is_json else {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "'title' is required"}), 400
    return jsonify(planner_tools.add_itinerary_item(
        trip_id, title, day_date=body.get("day_date"),
        destination_id=body.get("destination_id"), is_outdoor=bool(body.get("is_outdoor")),
        start_time=body.get("start_time"), end_time=body.get("end_time"), notes=body.get("notes"),
    )), 201


@app.route("/itinerary/items/<item_id>", methods=["DELETE"])
def delete_itinerary_item(item_id):
    return jsonify(planner_tools.remove_itinerary_item(item_id))


@app.route("/itinerary/items/<item_id>/move", methods=["POST"])
def move_itinerary_item(item_id):
    body = request.json if request.is_json else {}
    return jsonify(planner_tools.move_itinerary_item(
        item_id, day_date=body.get("day_date"), start_time=body.get("start_time"),
        end_time=body.get("end_time"), sort_order=body.get("sort_order"),
    ))


# ---------------------------------------------------------------------------
# In-app agent chat
# ---------------------------------------------------------------------------
@app.route("/agent/chat", methods=["POST"])
def agent_chat():
    """Send a message to the planner agent (tool-calling over planner_tools)."""
    body = request.json if request.is_json else {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"error": "'message' is required"}), 400
    result = agent.chat(message, trip_id=body.get("trip_id"), history=body.get("history"))
    return jsonify(result)


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", "8000"))
    app.run(debug=True, host=host, port=port)
