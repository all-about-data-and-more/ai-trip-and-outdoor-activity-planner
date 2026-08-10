# Screenshots — evidence the system works end to end

| File | What it shows | Component |
|------|---------------|-----------|
| `01_app.png` | Databricks App frontend: trips, destinations with Wikimedia descriptions, indoor/outdoor activities | 4 (app + frontend) |
| `02_weather_air.png` | Live weather + air-quality for a destination (Open-Meteo APIs) | 2 (third-party APIs) |
| `03_itinerary.png` | Agent-generated weather-aware itinerary — indoor picks on high rain chance; outdoor items flagged to reschedule | 5 (agent writes) |
| `04_search.png` | Semantic activity search over pgvector embeddings | 3 + 5 (embeddings + retrieval) |
| `05_agent_chat.png` | In-app chat agent (Foundation Model tool-calling loop) — agent surface #1 | 5 (agent) |
| `06_agent_mcp.png` | Agent Bricks agent calling `search_activities` over the FastMCP server — agent surface #2 | 5 (agent over MCP) |
| `07_spark_pipeline_1.png` | Spark pipeline: a `pandas_udf` embeds text chunks (activity/destination/user_note) at scale | 1 + 3 (Spark + embeddings) |
| `07_spark_pipeline_2.png` | Spark pipeline (continued): the embedded DataFrame written to the governed Delta gold table | 1 (Spark → Delta) |
| `08_analytics.png` | Spark writes `workspace.trip_planner.app_analytics` (Delta gold): engagement funnel + events-by-type | 6 (Delta app analytics) |
| `09_apps_overview.png` | All three apps deployed and **ACTIVE** in Databricks Apps | deployment |
| `10_packing.png` | Agent-built packing list (weather + activity derived) | 5 (agent capability) |

Captured against real, enriched destinations (`scripts/enrich_destinations.py` populates attractions).
