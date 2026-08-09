# Agent setup — Trip Planner (Agent Bricks + MCP)

How the Trip Planner MCP server is deployed and wired to a Databricks Agent Bricks agent. This is the
"Option 2" agent surface; the app also has an in-app chat agent (`agent.py`) over the *same*
`planner_tools.py`, so behavior is identical either way.

## 1. Deploy the MCP server app

Deploy this `mcp_server/` folder as its own Databricks App (source = this repo's Git folder, this
subfolder; `app.yaml` runs `python planner_mcp_server.py`). It needs the same `database/lakebase-url`
secret as the main app — nothing else (all data APIs are key-less). It serves MCP at:

`https://<your-mcp-app>-<workspace-id>.aws.databricksapps.com/mcp`

> **Shared code note:** each Databricks App deploys from its own folder, so `mcp_server/` keeps its
> own copies of `lakebase.py`, `embeddings.py`, `embedding_job.py`, `llm.py`, `planner_tools.py`, and
> `clients/`. If you edit any of those at the repo root, **copy them into `mcp_server/` too** (same
> convention as Day 3). A one-liner: `cp lakebase.py embeddings.py embedding_job.py llm.py
> planner_tools.py mcp_server/ && cp clients/*.py mcp_server/clients/`.

## 2. Register the MCP server in your workspace

Follow *Connect agents to external MCPs*:

1. **AI Gateway → MCPs → Add MCP** (or **Register external MCP**).
2. Paste the app's `/mcp` URL as the server endpoint (streamable HTTP) and name it `trip-planner`.
3. Databricks introspects it and lists the tools (`generate_itinerary`, `reschedule_for_weather`,
   `build_packing_list`, `search_activities`, `get_forecast`, `get_air_quality`, …).

## 3. Build the Agent Bricks agent

1. **Agents → Agent Bricks → Create agent** → **Custom LLM** (a tool-calling model such as
   *Meta Llama 3.3 70B Instruct*).
2. **Tools → Add tools → MCP server** → add `trip-planner` (all tools, or a curated subset).
3. **Add system prompt** → paste [`system_prompt.md`](system_prompt.md).
4. (Optional) Add a Genie space / UC function over the `trips`/`destinations` tables for extra context.

## 4. Export as a deployed agent app (optional)

With tools + prompt configured in the **AI Playground**, use **Get code → Export to Databricks Apps**
to scaffold and deploy the agent (OpenAI Agents SDK). It binds `trip-planner` as an app resource
(grants its service principal `CAN_USE` on the MCP server).

## 5. Sample prompts to test

- "Create-free: I have a trip to Kyoto and Osaka next week — build me a day-by-day itinerary."
- "Will it rain in Kyoto this week? Move any outdoor activities off the rainy days and tell me why."
- "What should I pack?"
- "Find me quiet gardens and temples for this trip."
- "What's the air quality like in Osaka on the 12th?"
- "Explain the changes you made to my plan."
