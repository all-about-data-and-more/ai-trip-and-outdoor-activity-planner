# Architecture

## Components

- **Flask app (`app.py`)** — the Databricks App. Thin REST routes + the single-page UI
  (`templates/index.html`). Delegates all logic to `planner_tools.py`.
- **Shared tool layer (`planner_tools.py`)** — every user/trip/destination operation, weather + air
  snapshots, and vector retrieval. The **same** module the agent (in-app chat and MCP) calls, so the
  UI and the agent can never drift.
- **API clients (`clients/`)** — key-less adapters: Open-Meteo geocoding/weather/air-quality +
  Wikimedia. All HTTP + parsing lives here; callers get clean dicts.
- **Embeddings (`embeddings.py`, `embedding_job.py`)** — MiniLM (384-dim) chunk + embed, shared by
  the app's `/embeddings/refresh` and the Spark notebook.
- **Lakebase (Postgres + pgvector)** — the 7 capstone tables + `place_embeddings`. `lakebase.py`
  owns the connection (env `LAKEBASE_URL` → secret fallback) and idempotent DDL.
- **Spark pipeline (`notebooks/ingest_place_embeddings.py`)** — batch embedding into a Delta gold
  table; see `spark_pipeline_and_serverless.md`.
- **Agent (Phase 2)** — `agent.py` (in-app Foundation Model tool-calling) + `mcp_server/` (FastMCP
  for Agent Bricks), both over `planner_tools.py`.

## Request flow — "add a destination"

```
POST /trips/<id>/destinations {name: "Kyoto"}
  → planner_tools.add_destination
      → geocoding_client.geocode("Kyoto")            # Open-Meteo → lat/lon
      → wikimedia_client.get_summary("Kyoto")        # description + url
      → INSERT destinations
      → wikimedia_client.get_nearby_attractions(...)  # GeoSearch → POIs
      → INSERT activities (is_outdoor classified)
  → (later) POST /embeddings/refresh
      → embedding_job.run  → chunk + embed → INSERT place_embeddings (pgvector)
  → POST /search {query}
      → planner_tools.search_activities  → cosine <=> over place_embeddings
```

## Data model relationships

```
users (email)
  └─< trips (user_email)
        ├─< destinations (trip_id) ──< activities (destination_id)
        ├─< itinerary_items (trip_id, destination_id, activity_id)
        ├─< packing_items (trip_id)
        └   weather_snapshots (destination_id, snapshot_date)   ← one per destination/day

place_embeddings (source_type, source_id, trip_id) ── polymorphic over
      destinations.description | activities.description+requirements | trips.notes
```

## Why one shared tool layer

The capstone needs an agent that both retrieves and writes. Rather than duplicate that logic between
the UI endpoints and the agent, both call `planner_tools.py`. The MCP server and the in-app chat are
thin wrappers; the app's REST routes are thin wrappers. One implementation, three surfaces.
