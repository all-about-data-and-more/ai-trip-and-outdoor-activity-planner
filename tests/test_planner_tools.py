#!/usr/bin/env python
"""
Integration smoke test for the Trip Planner tool layer.

Exercises the full agent tool path — create trip → add destination (geocode +
Wikimedia) → weather+AQI snapshot → embed → retrieve → generate itinerary →
reschedule for weather → packing list → explain → itinerary CRUD — against a
live Lakebase, then cleans up everything it created.

This is an INTEGRATION test (like Day 3's test_watchlist.py): it needs a
reachable Lakebase and outbound internet (Open-Meteo + Wikimedia). Run it with
the project root on PYTHONPATH and a Databricks profile (or LAKEBASE_URL) set:

    # from the repo root
    DATABRICKS_CONFIG_PROFILE=weather-app python tests/test_planner_tools.py
"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import embedding_job  # noqa: E402
import lakebase  # noqa: E402
import planner_tools as pt  # noqa: E402

EMAIL = "smoketest@example.com"


def _cleanup(trip_id: str) -> None:
    dids = [r["id"] for r in lakebase.run_query(
        "SELECT id FROM destinations WHERE trip_id=%s", (trip_id,))]
    lakebase.run_write("DELETE FROM place_embeddings WHERE trip_id=%s", (trip_id,))
    if dids:
        lakebase.run_write("DELETE FROM weather_snapshots WHERE destination_id = ANY(%s)", (dids,))
        lakebase.run_write("DELETE FROM activities WHERE destination_id = ANY(%s)", (dids,))
    for tbl in ("itinerary_items", "packing_items", "destinations"):
        lakebase.run_write(f"DELETE FROM {tbl} WHERE trip_id=%s", (trip_id,))
    lakebase.run_write("DELETE FROM trips WHERE id=%s", (trip_id,))
    lakebase.run_write("DELETE FROM users WHERE email=%s", (EMAIL,))


def main() -> int:
    print("=" * 60, "\nTrip Planner tool-layer smoke test\n", "=" * 60, sep="")
    start = dt.date.today() + dt.timedelta(days=1)
    end = start + dt.timedelta(days=2)
    checks: list[tuple[str, bool]] = []
    trip_id = None
    try:
        trip = pt.create_trip(EMAIL, "Smoke test trip",
                              start_date=start.isoformat(), end_date=end.isoformat(),
                              notes="Love hiking and gardens")
        trip_id = trip["id"]
        checks.append(("create_trip", bool(trip_id)))

        dest = pt.add_destination(trip_id, "Kyoto, Japan")
        checks.append(("add_destination (+activities)", dest.get("activities_created", 0) > 0))

        snap = pt.snapshot_weather(dest["id"], days=5)
        checks.append(("snapshot_weather", len(snap.get("snapshots", [])) > 0))

        emb = embedding_job.run()
        checks.append(("embeddings refresh", emb.get("rows_written", 0) > 0))

        found = pt.search_activities("temples and gardens", trip_id=trip_id)
        checks.append(("search_activities", found.get("count", 0) > 0))

        itin = pt.generate_itinerary(trip_id)
        checks.append(("generate_itinerary", itin.get("items_created", 0) > 0))

        resched = pt.reschedule_for_weather(trip_id)
        checks.append(("reschedule_for_weather", "changes_made" in resched))

        pack = pt.build_packing_list(trip_id)
        checks.append(("build_packing_list", pack.get("items_created", 0) > 0))

        expl = pt.explain_changes(trip_id)
        checks.append(("explain_changes", bool(expl.get("explanation"))))

        item = pt.add_itinerary_item(trip_id, "Custom dinner", day_date=end.isoformat())
        pt.move_itinerary_item(item["id"], day_date=start.isoformat())
        removed = pt.remove_itinerary_item(item["id"])
        checks.append(("itinerary item add/move/remove", removed.get("removed", 0) == 1))
    finally:
        if trip_id:
            _cleanup(trip_id)
            print("\n(cleaned up all test rows)")

    print()
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
