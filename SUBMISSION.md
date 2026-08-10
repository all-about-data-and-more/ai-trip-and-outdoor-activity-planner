# Submission — AI Trip &amp; Outdoor Activity Planner (Capstone)

**Student:** Samuel Shaibu
**Assignment:** Capstone — AI Trip &amp; Outdoor Activity Planner

A weather- and air-quality-aware trip planner on **Databricks + Lakebase (Postgres + pgvector)**.
Users save destinations and preferences; an AI agent builds a day-by-day itinerary, reschedules
outdoor activities on rain / poor air quality, builds a packing list, edits the plan, and explains
its weather-based decisions — grounded in real forecasts and its own vector search.

## Links &amp; deployed apps

- **GitHub repo:** https://github.com/modern-data-engineering-lab/ai-trip-and-outdoor-activity-planner
- **Main app** (`ai-trip-planner-app`): https://ai-trip-planner-app-7474650707148987.aws.databricksapps.com
- **MCP server app** (`mcp-trip-planner`): https://mcp-trip-planner-7474650707148987.aws.databricksapps.com
  — MCP endpoint: `…/mcp`
- **Agent app** (`agent-trip-planner`): https://agent-trip-planner-7474650707148987.aws.databricksapps.com

## The 6 mandatory components → where satisfied

| # | Component | Where |
|---|-----------|-------|
| 1 | **Data pipeline in Spark** | `notebooks/ingest_place_embeddings.py` — Spark DataFrame + a `@pandas_udf` (`:142`) embed place/attraction/note text at scale into a governed **Delta gold** table (window-function chunking + `saveAsTable`). |
| 2 | **Integration with ≥1 third-party API** | Four **key-less** APIs in `clients/`: Open-Meteo Geocoding (`geocoding_client.py`), Weather (`weather_client.py`), Air Quality (`air_quality_client.py`), and Wikimedia summaries + GeoSearch (`wikimedia_client.py`). |
| 3 | **Unstructured-data processing** | Destination descriptions, attraction text, activity requirements, and user notes are chunked + embedded (384-dim MiniLM) into `place_embeddings` (pgvector) — `embedding_job.py`, `embeddings.py`. |
| 4 | **Databricks App with a frontend** | `app.py` (Flask REST) + `templates/index.html` (single-page UI), deployed via `app.yaml` as `ai-trip-planner-app`. |
| 5 | **Agent that searches/retrieves AND writes to the DB** | `planner_tools.py` — retrieval (`search_activities:374`, pgvector cosine) **and** writes (`add_destination:193`, `generate_itinerary:537`, `reschedule_for_weather:620`, `build_packing_list:671`). Exposed via an in-app agent **and** an Agent Bricks agent over MCP. |
| 6 | **Analytics inside a Delta table about the app** | `analytics.py` logs usage events (trip created, itinerary generated, reschedule, search, agent chat, …) to the Lakebase `app_events` table (fire-and-forget). `notebooks/app_analytics.py` (Spark) reads them via JDBC, mirrors to a **Delta** bronze table, and writes a **Delta gold** table `workspace.trip_planner.app_analytics` (event mix, engagement funnel, daily activity) with charts. |

## Data &amp; auth (no secrets committed)

Every data source is **key-less** (Open-Meteo + Wikimedia — no signup, no API key). The **only**
secret is the Lakebase connection URL, stored in the `database/lakebase-url` Databricks secret
(`setup_secrets.py`); `.env` is git-ignored. So "no hardcoded keys / no secrets committed" is met by
construction.

## Lakebase tables (the 7 required + a vector store)

`users` · `trips` · `destinations` · `activities` · `itinerary_items` · `weather_snapshots` ·
`packing_items` · **`place_embeddings`** (`VECTOR(384)`, HNSW cosine). All created idempotently by
`lakebase.ensure_all_tables():315`; reference DDL in `sql/`.

## Context engineering → where satisfied

| Requirement | Where |
|-------------|-------|
| Embed destination descriptions | `embedding_job.fetch_unembedded_sources` (`destination` rows) |
| Embed attraction information | Wikimedia GeoSearch → `activities` (source `wikimedia`) → embedded |
| Embed activity requirements | `activities.requirements` combined into the embedded text |
| Embed user notes | `trips.notes` → `user_note` embeddings |
| Retrieve suitable activities by interest + conditions | `planner_tools.search_activities(query, outdoor_only=…):374` (pgvector `<=>`) |

## Agent capabilities (from the project spec) → where satisfied

| Capability | Where |
|------------|-------|
| Generate a day-by-day itinerary | `generate_itinerary:537` → `itinerary_items` (weather-aware) |
| Reschedule outdoor activities on rain / poor AQI | `reschedule_for_weather:620` (reads `weather_snapshots`, records reasons) |
| Build a packing list | `build_packing_list:671` → `packing_items` (weather + activity derived) |
| Add / remove / move itinerary items | `add_itinerary_item` / `remove_itinerary_item` / `move_itinerary_item` (+ `reorder_itinerary`) |
| Explain each weather-based change | `explain_changes:809` (Foundation Model over the recorded reasons; deterministic fallback) |

## The agent — two surfaces, one tool layer

Both call the **same** `planner_tools.py`, so they never diverge:

- **In-app chat agent** — `agent.py::chat:114`: a Databricks Foundation Model **tool-calling loop**
  over `planner_tools`, with a deterministic keyword-routing fallback (`_fallback:163`) if the
  endpoint can't tool-call on Free Edition (`llm.tool_chat:64`). Route: `POST /agent/chat`.
- **FastMCP server + Agent Bricks** — `mcp_server/planner_mcp_server.py`: 15 thin `@mcp.tool`
  wrappers over `planner_tools`, served over streamable-HTTP (`mcp.run(transport="http", …):154`),
  registered as an external MCP for a Databricks Agent Bricks agent
  (`agent-trip-planner`). Setup: `mcp_server/agent/agent_setup.md`; prompt: `…/system_prompt.md`.

The agent both **retrieves** (search / weather / air-quality tools) and **writes**
(itinerary / reschedule / packing / destination tools).

## Key engineering decisions / platform constraints handled (diagnosed, not hidden)

| Constraint (Databricks Free-edition Serverless) | Handling |
|---|---|
| `psycopg2` / Spark-JDBC can't write Lakebase from Serverless | **Two-phase embeddings** — Spark → Delta gold (Serverless-safe), App → pgvector serving copy. `docs/spark_pipeline_and_serverless.md` |
| Tables created by a different Postgres role → "must be owner" on `CREATE INDEX` | **Best-effort index DDL** — `lakebase._try_ddl:122` skips-and-logs instead of failing the request (DML works via `pg_write_all_data`). |
| Databricks injects secret-backed env vars base64-encoded (double-encoded here) | **Multi-layer decode** — `lakebase._maybe_decode:56` peels base64 layers until a `postgresql://` URL surfaces. |
| Serverless DNS can't resolve `en.wikipedia.org` | **Off-platform enrichment** — `scripts/enrich_destinations.py` runs Wikimedia enrichment from a box with egress and writes into Lakebase. `docs/serverless_egress_and_enrichment.md` |
| A model endpoint may not support tool-calling | **Graceful fallback** — the in-app agent degrades to deterministic routing; never hard-fails. |

## Error handling &amp; edge cases

- Tools return clean dicts, never stack traces — MCP tools wrap every call in `_safe(...)` →
  `{"status":"error","message":…}` (`mcp_server/planner_mcp_server.py`); the app has a JSON error
  handler (`app.py`) so the frontend never chokes on an HTML error page.
- Unknown place → `geocoding_client.geocode` raises `ValueError` → surfaced as a clean error.
- Empty query / missing trip → validated, `400`/`404` with a message.
- `days` clamped to sane ranges (`weather_client.get_forecast`, `air_quality_client`).
- Wikimedia unreachable → best-effort: destination still saves; failure is **logged** (not swallowed).
- Delete is **cascading** (`delete_trip` / `delete_destination`) — no orphaned rows (verified live).

## Stretch / beyond the brief

- **Two agent surfaces** (in-app chat *and* Agent Bricks over MCP), not one.
- **Full CRUD UI** — edit/delete trips, delete destinations, add custom activities, reorder itinerary
  items, interactive packing checklist.
- **Markdown export** of the itinerary + packing list.
- **Air quality + UV + pollen** (not just weather) drive rescheduling and packing.
- **Integration smoke test** (`tests/test_planner_tools.py`) exercising the whole tool path against
  live Lakebase with cleanup.

## Code map (file:line — nothing left to inference)

- Spark pipeline + `pandas_udf`: `notebooks/ingest_place_embeddings.py:142`
- pgvector retrieval: `planner_tools.py:374` (`search_activities`, `embedding <=> %s::vector`)
- Agent writes: `add_destination:193`, `generate_itinerary:537`, `reschedule_for_weather:620`, `build_packing_list:671`, `explain_changes:809`
- In-app tool-calling agent + fallback: `agent.py:114` (`chat`), `agent.py:163` (`_fallback`), `llm.py:64` (`tool_chat`)
- MCP server (FastMCP, streamable-HTTP, 15 `@mcp.tool`): `mcp_server/planner_mcp_server.py` (`mcp.run(...):154`)
- Schema (7 tables + pgvector): `lakebase.py:315` (`ensure_all_tables`); DDL in `sql/`
- App analytics → Delta: `analytics.py` (`log_event` → Lakebase `app_events`); `notebooks/app_analytics.py` (Spark JDBC read → Delta bronze `app_events_delta` + gold `app_analytics` + charts)
- Constraint handling: `_maybe_decode:56`, `_try_ddl:122`, `scripts/enrich_destinations.py`
- Requirements traceability: `docs/capstone_requirements_mapping.md`

## Files

- `app.py` — Flask app: REST + `/agent/chat` + the frontend
- `planner_tools.py` — the shared tool layer (retrieval + writes) both agents call
- `agent.py` / `llm.py` — in-app tool-calling agent + Foundation Model helpers
- `lakebase.py` / `sql/` — connection + schema (7 tables + pgvector)
- `embeddings.py` / `embedding_job.py` — chunk + embed helpers
- `clients/` — Open-Meteo (geocoding/weather/air-quality) + Wikimedia adapters
- `notebooks/ingest_place_embeddings.py` — the Spark pipeline
- `analytics.py` — app-usage events → Lakebase `app_events`
- `notebooks/app_analytics.py` — Spark (JDBC) → Delta bronze + gold analytics + charts
- `mcp_server/` — FastMCP server + Agent Bricks system prompt + setup
- `scripts/enrich_destinations.py` — off-platform Wikimedia enrichment
- `tests/test_planner_tools.py` — integration smoke test
- `templates/index.html` — single-page UI
- `README.md` — full write-up; `docs/` — architecture, requirements mapping, constraint docs
- `screenshots/` (`docs/screenshots/`) — the app + agents working

## Screenshots (evidence)

See `docs/screenshots/` — each mapped to the component it proves:

| File | Shows | Component |
|------|-------|-----------|
| `01_app.png` | App frontend: trips, destinations (Wikimedia), activities | 4 |
| `02_weather_air.png` | Live weather + air quality (Open-Meteo) | 2 |
| `03_itinerary.png` | Weather-aware itinerary (indoor picks on rain; outdoor flagged) | 5 (writes) |
| `04_search.png` | Semantic activity search over pgvector | 3 + 5 (retrieval) |
| `05_agent_chat.png` | In-app chat agent (tool-calling) — surface #1 | 5 |
| `06_agent_mcp.png` | Agent Bricks agent calling `search_activities` over MCP — surface #2 | 5 |
| `07_spark_pipeline_1.png` / `_2.png` | Spark `pandas_udf` embeds chunks → governed Delta gold | 1 + 3 |
| `08_analytics.png` | Spark writes `app_analytics` Delta gold (funnel + events-by-type) | 6 |
| `09_apps_overview.png` | All three apps deployed and ACTIVE | deployment |
| `10_packing.png` | Agent-built packing list | 5 |
