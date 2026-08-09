# Serverless Egress & Off-Platform Enrichment

## The constraint

Databricks **Free-edition Serverless** (which runs Apps *and* notebooks here) has a **restricted
DNS/egress allowlist**. Some external hosts resolve; others don't. Concretely, from the deployed app:

| Host | Used for | Resolves from the app? |
|---|---|---|
| `huggingface.co` | download the embedding model | ✅ yes |
| `*.open-meteo.com` | weather, air quality, geocoding | ✅ yes |
| `en.wikipedia.org` | destination descriptions + attractions | ❌ **no** |

When `add_destination` runs inside the app, its Wikimedia calls fail with:

```
Failed to resolve 'en.wikipedia.org' ([Errno -3] Temporary failure in name resolution)
```

so destinations come back with **no description and no activities**. This is the same class of block
seen in Day 2 (serverless couldn't resolve article-body URLs) and Day 3 (Alpaca's host wouldn't
resolve). It is a **networking limit, not a code bug** — and Free Edition provides no way to add a
host to the serverless egress allowlist (that needs a paid workspace's Network Connectivity Config).

> **The differential test that proves it's host-specific** (worth remembering for any external tool):
> weather works (`api.open-meteo.com` resolves) but attractions don't (`en.wikipedia.org` doesn't).
> One host up, another down ⇒ the block is that *host*, not a blanket "no egress".

## The fix — enrich off-platform, serve from Lakebase

Exactly like Day 2's chunk embeddings: do the Wikimedia enrichment on a machine that **can** reach
Wikimedia (your laptop, Colab, any box with normal egress), and write the results into Lakebase. The
app then just reads them — no app-side Wikimedia call needed.

```bash
# from the repo root, with your Databricks profile (or LAKEBASE_URL) set
DATABRICKS_CONFIG_PROFILE=weather-app python scripts/enrich_destinations.py
```

`scripts/enrich_destinations.py` finds destinations missing a description/activities, fetches the
Wikimedia summary + nearby attractions, writes `destinations.description` and `activities`, and
refreshes `place_embeddings`. Then click **Generate itinerary** in the app — it now plans over real
attractions, and **Reschedule for weather** has real *outdoor* activities to move.

### Flow

```
add destination in the app  ──▶  destinations row (geocode + weather work; Wikimedia does not)
                                        │
        scripts/enrich_destinations.py  │  (run from a box with Wikimedia egress)
                                        ▼
             destinations.description + activities + place_embeddings  (in Lakebase)
                                        │
                         the app reads them  ──▶  itinerary over real attractions
```

## Alternatives

- **Colab / any VM with egress** — same script, set `LAKEBASE_URL` (see `.env.example`).
- **A paid Databricks workspace** — add `en.wikipedia.org` to a serverless **Network Connectivity
  Config** egress rule; then `add_destination` works entirely in-app with no enrichment step.
- **Scheduled enrichment** — run `enrich_destinations.py` on a cron/Job from a box with egress so new
  destinations get enriched automatically.

## Why weather still works in-app

Open-Meteo's hosts are reachable, so `snapshot_weather`, `get_forecast`, `get_air_quality`,
`generate_itinerary`, `reschedule_for_weather`, and `build_packing_list` all run fully in the deployed
app. Only the **Wikimedia** enrichment needs the off-platform step.
