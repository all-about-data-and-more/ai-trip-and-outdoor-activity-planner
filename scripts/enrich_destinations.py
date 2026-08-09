"""
Off-platform destination enricher.

Databricks Free-edition Serverless can't resolve `en.wikipedia.org` (DNS is
restricted to an egress allowlist), so `add_destination` running inside the
deployed app gets no Wikimedia description or nearby attractions — the same
class of block Day 2 hit fetching article bodies. This script runs the Wikimedia
enrichment from a machine that CAN reach Wikimedia (your laptop, Colab, any box
with normal egress) and writes the results straight into Lakebase, which the app
then reads. See docs/serverless_egress_and_enrichment.md.

For every destination missing a description or activities, it:
  1. fetches the Wikimedia summary  -> destinations.description / wiki_url
  2. fetches nearby attractions      -> activities (classified indoor/outdoor)
  3. refreshes embeddings            -> place_embeddings (so search works)

Usage (from the repo root, with a Databricks profile or LAKEBASE_URL set):
    DATABRICKS_CONFIG_PROFILE=weather-app python scripts/enrich_destinations.py
    python scripts/enrich_destinations.py --trip <trip_id>      # one trip only
    python scripts/enrich_destinations.py --all                 # re-enrich everything
"""

import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import embedding_job  # noqa: E402
import lakebase  # noqa: E402
import planner_tools  # noqa: E402
from clients import wikimedia_client  # noqa: E402


def _targets(trip_id: str | None, do_all: bool) -> list[dict]:
    """Destinations to enrich: those missing activities (default), or all."""
    where = "WHERE d.trip_id = %s" if trip_id else ""
    params = (trip_id,) if trip_id else None
    rows = lakebase.run_query(
        f"""
        SELECT d.id, d.name, d.lat, d.lon,
               (SELECT COUNT(*) FROM {lakebase.ACTIVITIES_TABLE} a
                WHERE a.destination_id = d.id) AS n_acts,
               (d.description IS NOT NULL) AS has_desc
        FROM {lakebase.DESTINATIONS_TABLE} d {where}
        ORDER BY d.created_at DESC
        """,
        params,
    )
    if do_all:
        return rows
    return [r for r in rows if r["n_acts"] == 0 or not r["has_desc"]]


def enrich_destination(dest: dict, max_attractions: int = 10) -> int:
    """Enrich one destination in place. Returns the number of activities added."""
    # Wikimedia title = the leading part of the stored label ("Kyoto, Kyoto, Japan" -> "Kyoto").
    title = dest["name"].split(",")[0].strip()
    try:
        summary = wikimedia_client.get_summary(title)
        lakebase.run_write(
            f"UPDATE {lakebase.DESTINATIONS_TABLE} SET description = %s, wiki_url = %s WHERE id = %s",
            (summary.get("extract"), summary.get("url"), dest["id"]),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    (no description for {title!r}: {exc})")

    if dest.get("lat") is None or dest.get("lon") is None:
        print(f"    (no coordinates for {dest['name']!r}; skipping attractions)")
        return 0

    attractions = wikimedia_client.get_nearby_attractions(
        dest["lat"], dest["lon"], limit=max_attractions
    )
    added = 0
    for att in attractions:
        is_outdoor = planner_tools._guess_outdoor(att["title"], att.get("extract"))
        lakebase.run_write(
            f"""
            INSERT INTO {lakebase.ACTIVITIES_TABLE}
                (id, destination_id, name, category, is_outdoor, description, tags, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'wikimedia')
            ON CONFLICT (id) DO NOTHING
            """,
            (uuid.uuid4().hex, dest["id"], att["title"],
             "outdoor" if is_outdoor else "indoor", is_outdoor,
             att.get("extract"), json.dumps([])),
        )
        added += 1
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trip", help="Only enrich destinations for this trip_id.")
    ap.add_argument("--all", action="store_true", help="Re-enrich every destination.")
    ap.add_argument("--max-attractions", type=int, default=10)
    args = ap.parse_args()

    targets = _targets(args.trip, args.all)
    if not targets:
        print("Nothing to enrich — every destination already has a description and activities.")
        return 0

    print(f"Enriching {len(targets)} destination(s) via Wikimedia...")
    total = 0
    for dest in targets:
        n = enrich_destination(dest, args.max_attractions)
        total += n
        print(f"  {dest['name']} -> +{n} activities")

    print("Refreshing embeddings...")
    result = embedding_job.run()
    print(f"  embedded {result['sources_embedded']} sources -> {result['rows_written']} chunks")
    print(f"\nDone: {total} activities added across {len(targets)} destinations.")
    print("Now click 'Generate itinerary' in the app to rebuild the plan with real activities.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
