# 🧭 AI Trip &amp; Outdoor Activity Planner

A weather- and air-quality-aware trip planner built on **Databricks + Lakebase**. Users save
destinations and preferences; an **AI agent** builds a day-by-day itinerary, **reschedules outdoor
activities** when rain or poor air quality is forecast, generates a **packing list**, edits itinerary
items, and **explains every weather-based decision** — grounding all of it in real forecasts and its
own vector search over destination/attraction/activity descriptions.

This is the capstone of a Databricks AI bootcamp. It is the synthesis of the earlier projects:
Lakebase + pgvector retrieval (weather app), the FastMCP + Agent Bricks pattern (Day 3), key-less
Open-Meteo clients (weather-mcp-server), and a Spark embedding pipeline (Day 2).

> **Status:** Phase 1 + Phase 2 complete — data model, key-less API clients, Spark embedding
> pipeline, retrieval, the Flask app + UI, itinerary/reschedule/packing/explain logic, an **in-app
> chat agent**, and a **FastMCP server** for Agent Bricks. All tool paths validated end-to-end
> against live Lakebase.

## What it does (mapped to the bootcamp's 5 required components)

| Required component | How this project satisfies it |
|---|---|
| **1. Spark data pipeline** | [`notebooks/ingest_place_embeddings.py`](notebooks/ingest_place_embeddings.py) — Spark DataFrame + `pandas_udf` embeds place/attraction/note text at scale into a governed Delta gold table. |
| **2. Third-party API integration** | Four **key-less** APIs in [`clients/`](clients/): Open-Meteo Geocoding, Weather, and Air Quality, plus Wikimedia summaries + GeoSearch. |
| **3. Unstructured-data processing** | Destination descriptions, attraction/activity text, and user notes are chunked + embedded (384-dim MiniLM) into `place_embeddings` (pgvector). |
| **4. Databricks App with a frontend** | [`app.py`](app.py) + [`templates/index.html`](templates/index.html) — a single-page trip planner served as a Databricks App. |
| **5. Agent that retrieves AND writes** | [`planner_tools.py`](planner_tools.py) — retrieval (`search_activities`, pgvector) **and** writes (`add_destination`, `snapshot_weather`, and in Phase 2 `generate_itinerary`/`reschedule_for_weather`/`build_packing_list`). Surfaced via an in-app chat agent and a FastMCP server for Agent Bricks. |

Full traceability is in [`docs/capstone_requirements_mapping.md`](docs/capstone_requirements_mapping.md).

## Architecture

```
                         ┌──────────────────── Databricks App (Flask) ────────────────────┐
  Browser  ────────────▶ │  app.py  ──▶  planner_tools.py  ──▶  Lakebase (Postgres+pgvector) │
   (index.html)          │      │              │        │                                    │
                         │      │              │        └──▶ clients/ ──▶ Open-Meteo, Wikimedia
                         │      └─ /agent/chat ─┴─ agent.py (Foundation Model tool-calling)  │  ← Phase 2
                         └────────────────────────────────────────────────────────────────┘
                                        ▲
   Agent Bricks agent ──(MCP tool calls)┘   mcp_server/planner_mcp_server.py (same planner_tools)  ← Phase 2

   Spark: notebooks/ingest_place_embeddings.py ──▶ Delta gold ──(/embeddings/refresh)──▶ pgvector
```

See [`docs/architecture.md`](docs/architecture.md) for the data flow and table relationships.

## Data model (Lakebase / Postgres)

The seven required tables plus a pgvector context store, all created idempotently by
`lakebase.ensure_all_tables()` (reference DDL in [`sql/`](sql/)):

`users` · `trips` · `destinations` · `activities` · `itinerary_items` · `weather_snapshots` ·
`packing_items` · **`place_embeddings`** (VECTOR(384), HNSW cosine index).

## Setup

### 1. Lakebase + secret
Reuse your existing Lakebase instance (or create one per the Day-2 README). Store its connection URL
as the `database/lakebase-url` secret — the only secret this app needs (every data API here is
key-less):

```bash
python setup_secrets.py     # prompts for the Lakebase URL, stores it base64-encoded
```

### 2. Local run
```bash
pip install -r requirements.txt
cp .env.example .env         # set LAKEBASE_URL, or rely on a configured Databricks CLI profile
python app.py                # http://localhost:8000
```

### 3. Deploy as a Databricks App
Push to your Git folder, then **Compute → Apps → Create app → Custom**, point it at this folder (it
reads `app.yaml`), and Deploy. Open the app URL and create a trip.

### 4. Run the Spark pipeline (optional, for the batch/Delta path)
Import `notebooks/ingest_place_embeddings.py` and run it on a Serverless notebook — it writes the
Delta gold embeddings table. Then call `POST /embeddings/refresh` on the app to load vectors into
Lakebase for serving. Why the split? See [`docs/spark_pipeline_and_serverless.md`](docs/spark_pipeline_and_serverless.md).

## API

| Method + path | Purpose |
|---|---|
| `GET /healthz` | health check |
| `GET/POST /trips`, `GET/DELETE /trips/<id>` | manage trips |
| `GET/POST /trips/<id>/destinations` | list / add a destination (auto Wikimedia desc + attractions) |
| `GET /destinations/<id>/activities` | activities for a destination |
| `POST /destinations/<id>/weather` | fetch forecast + AQI, upsert `weather_snapshots` |
| `GET /weather?location=`, `GET /air?location=` | raw forecast / air-quality reads |
| `POST /embeddings/refresh` | chunk + embed new text into `place_embeddings` |
| `POST /search` | semantic search over embedded activities |
| `GET /trips/<id>/itinerary`, `POST …/itinerary/generate` | list / build the weather-aware itinerary |
| `POST /trips/<id>/reschedule` | move outdoor items off rainy / poor-AQI days |
| `GET/POST /trips/<id>/packing` | list / build the packing list |
| `GET /trips/<id>/explain` | plain-language reasons for weather-based changes |
| `POST /trips/<id>/itinerary/items`, `DELETE /itinerary/items/<id>`, `POST …/<id>/move` | itinerary item CRUD |
| `POST /agent/chat` | talk to the in-app planner agent |

## The agent (two surfaces, one tool layer)

Both surfaces call the same `planner_tools.py`, so they behave identically:

- **In-app chat** — `agent.py` + `POST /agent/chat`: a Databricks Foundation Model tool-calling loop
  (with a deterministic keyword-routing fallback if the endpoint can't tool-call on Free Edition).
- **FastMCP server** — `mcp_server/`: exposes the tools over MCP for a Databricks **Agent Bricks**
  agent. Deploy it as a second app and follow [`mcp_server/agent/agent_setup.md`](mcp_server/agent/agent_setup.md).

> `mcp_server/` keeps its own copies of the shared modules (each Databricks App deploys from its own
> folder). If you edit a shared module at the repo root, copy it into `mcp_server/` too:
> `cp lakebase.py embeddings.py embedding_job.py llm.py planner_tools.py mcp_server/ && cp clients/*.py mcp_server/clients/`

## Tests

`tests/test_planner_tools.py` is an integration smoke test that runs the whole tool path
(create → add destination → weather → embed → retrieve → itinerary → reschedule → packing → explain →
CRUD) against a live Lakebase and cleans up after itself:

```bash
DATABRICKS_CONFIG_PROFILE=weather-app python tests/test_planner_tools.py
```

## Credits &amp; data sources

Open-Meteo (weather, air quality, geocoding) and Wikimedia — free for non-commercial use, no API key.
Built on the Databricks Lakebase + Apps + Foundation Model stack.
