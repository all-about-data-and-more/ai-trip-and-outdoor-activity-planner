# Capstone Requirements → Implementation Mapping

A traceability matrix so a reviewer (or the AI grader) can verify every required component in one
pass. Each row names the exact file + symbol that satisfies it.

## The 6 mandatory bootcamp components

### 1. A data pipeline in Spark
- **`notebooks/ingest_place_embeddings.py`** — a Spark job that reads raw place/attraction/note text
  (Delta bronze), chunks it with a Spark UDF + `explode`, embeds each chunk with a **`pandas_udf`**
  (MiniLM, distributed across executors), and writes a governed **Delta gold** table
  (`place_embeddings_gold`) via `saveAsTable`. Uses the DataFrame API, window functions, and a pandas
  UDF — genuine distributed Spark, Serverless-safe.

### 2. Integration with ≥1 third-party API
Four **key-less** integrations (no secrets needed), each an adapter in `clients/`:
- **Open-Meteo Geocoding** — `clients/geocoding_client.py::geocode` (name → lat/lon).
- **Open-Meteo Weather** — `clients/weather_client.py::get_forecast` / `get_current`.
- **Open-Meteo Air Quality** — `clients/air_quality_client.py::get_air_quality` (AQI, PM, UV, pollen).
- **Wikimedia** — `clients/wikimedia_client.py::get_summary` (descriptions) + `get_nearby_attractions`
  (GeoSearch points of interest).

### 3. Processing of unstructured data (embeddings)
- **`embedding_job.py`** + **`embeddings.py`** — destination descriptions (Wikimedia), activity /
  attraction descriptions + requirements, and user trip notes are chunked (sliding window) and
  embedded (384-dim `all-MiniLM-L6-v2`) into **`place_embeddings`** (pgvector). Retrieval:
  `planner_tools.py::search_activities` (cosine `<=>`).

### 4. A Databricks App with a frontend
- **`app.py`** (Flask REST + routes) + **`templates/index.html`** (single-page trip planner UI),
  deployed via **`app.yaml`** as a Databricks App. Uses the `X-Forwarded-Email` identity + JSON error
  handler conventions from the bootcamp's weather app.

### 5. An agent that searches/retrieves AND writes to the database
- **`planner_tools.py`** is the shared tool layer.
  - **Retrieves:** `search_activities` (pgvector), `get_forecast`, `get_air_quality`, `get_trip`,
    `list_destinations`, `list_itinerary`.
  - **Writes:** `add_destination` (inserts `destinations` + `activities`), `snapshot_weather`
    (upserts `weather_snapshots`), `generate_itinerary`, `reschedule_for_weather`,
    `add/remove/move_itinerary_item`, `build_packing_list`.
- Surfaced two ways, both over the same tool layer:
  - **In-app chat agent** — `agent.py` + `POST /agent/chat`: a Foundation Model tool-calling loop
    (with a deterministic keyword-routing fallback if the endpoint can't tool-call).
  - **FastMCP server** — `mcp_server/planner_mcp_server.py`: exposes the tools over MCP for a
    Databricks **Agent Bricks** agent (see `mcp_server/agent/agent_setup.md`).

### 6. Analytics inside a Delta table about your app
- **`analytics.py`** — the app logs usage events (trip created, destination added, itinerary
  generated, reschedule, packing built, search, agent chat) into a Unity Catalog **Delta** table
  `workspace.trip_planner.app_events` via the Databricks **SQL Statement Execution API**
  (fire-and-forget, best-effort — never blocks a request). Wired into the routes in `app.py`.
- **`notebooks/app_analytics.py`** — a **Spark** notebook reads the Delta events table, computes
  usage analytics (event mix, daily activity, an engagement funnel, top users) with charts, and
  writes a governed **Delta gold** table `workspace.trip_planner.app_analytics`.

## The project's own "Agent capabilities" spec

| Capability | Implementation |
|---|---|
| Generate a day-by-day itinerary | `planner_tools.generate_itinerary` → `itinerary_items` ✅ |
| Reschedule outdoor activities on rain / poor AQI | `planner_tools.reschedule_for_weather` (reads `weather_snapshots`) ✅ |
| Build a packing list | `planner_tools.build_packing_list` → `packing_items` ✅ |
| Add / remove / move itinerary items | `planner_tools.add/remove/move_itinerary_item` ✅ |
| Explain weather-based changes | `planner_tools.explain_changes` (Foundation Model over snapshots) ✅ |
| Retrieve suitable activities by interest + conditions | `planner_tools.search_activities(outdoor_only=…)` ✅ |

## The 7 required Lakebase tables

`users`, `trips`, `destinations`, `activities`, `itinerary_items`, `weather_snapshots`,
`packing_items` — all defined in `lakebase.py` (`ensure_*_table`) with reference DDL in
`sql/01_core_tables.sql`, plus `place_embeddings` (`sql/02_place_embeddings.sql`) for context
engineering.

## Context-engineering checklist (from the project spec)

- ✅ Embed destination descriptions — `embedding_job.fetch_unembedded_sources` (`destination` rows).
- ✅ Embed attraction information — same, `activity` rows sourced from Wikimedia GeoSearch.
- ✅ Embed activity requirements — `activities.requirements` combined into the embedded text.
- ✅ Embed user notes — `trips.notes` → `user_note` embeddings.
- ✅ Retrieve suitable activities by interests + conditions — `search_activities(query, outdoor_only)`.
