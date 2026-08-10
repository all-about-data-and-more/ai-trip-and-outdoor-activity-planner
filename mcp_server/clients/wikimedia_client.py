"""
Wikimedia client — destination descriptions + nearby attractions.

Two key-less endpoints power the unstructured text this app embeds:
  * REST summary  (/api/rest_v1/page/summary/{title}) -> a destination's
    short description + intro extract (the text stored on destinations.description).
  * GeoSearch     (/w/api.php?list=geosearch) -> nearby points of interest, then
    a batched `extracts` query for each one's intro text (the text stored on
    activities.description). These become the candidate "activities" the agent
    retrieves over.

Wikimedia asks every client to send a descriptive User-Agent, so we do.
No API key required.

    https://www.mediawiki.org/wiki/API:Geosearch
    https://en.wikipedia.org/api/rest_v1/
"""

import logging
import os
from urllib.parse import quote

import requests

logger = logging.getLogger("wikimedia")

_LANG = os.environ.get("WIKIMEDIA_LANG", "en")
_REST_BASE = os.environ.get("WIKIMEDIA_REST_BASE", "https://{lang}.wikipedia.org/api/rest_v1")
_API_BASE = os.environ.get("WIKIMEDIA_API_BASE", "https://{lang}.wikipedia.org/w/api.php")
_TIMEOUT = int(os.environ.get("WIKIMEDIA_HTTP_TIMEOUT", "12"))
_USER_AGENT = os.environ.get(
    "WIKIMEDIA_USER_AGENT",
    "AI-Trip-Planner/1.0 (bootcamp capstone; "
    "https://github.com/modern-data-engineering-lab/ai-trip-and-outdoor-activity-planner)",
)

_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

# GeoSearch near a city centroid returns lots of non-attractions (government
# offices, universities, stations, consulates, even historical *events*). Drop
# titles that clearly aren't places a traveler visits, so the itinerary is built
# from real attractions.
_EXCLUDE_KEYWORDS = (
    "station", "university", "college", "school", "consulate", "embassy",
    "prefecture", "agency", "incident", "ministry", "headquarters", "hospital",
    "office", "city council", "corporation", "company", "bombing", "attack",
    "disaster", "ward,", "district,", "line (", "high school",
    # events / disasters / non-places that geosearch sometimes returns
    "fire", "revolt", "rebellion", "battle of", "riot", "massacre",
    "earthquake", "flood", "war", "siege", "uprising", "raid on",
    "raid (", "expedition", "campaign", "shipwreck", "sinking of",
)


def _is_attraction(title: str) -> bool:
    t = (title or "").lower()
    return not any(k in t for k in _EXCLUDE_KEYWORDS)


def _rest_url() -> str:
    return _REST_BASE.format(lang=_LANG)


def _api_url() -> str:
    return _API_BASE.format(lang=_LANG)


def get_summary(title: str) -> dict:
    """
    Fetch a destination's description from the Wikimedia REST summary endpoint.

    Returns {title, description, extract, url, lat, lon}. Raises ValueError if
    the page doesn't exist, so the caller can fall back gracefully (e.g. keep
    the destination without a description rather than failing the whole save).
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("A destination title is required.")

    resp = requests.get(
        f"{_rest_url()}/page/summary/{quote(title, safe='')}",
        headers=_HEADERS,
        timeout=_TIMEOUT,
    )
    if resp.status_code == 404:
        raise ValueError(f"No Wikimedia page found for {title!r}.")
    resp.raise_for_status()
    data = resp.json() or {}
    coords = data.get("coordinates") or {}
    content_urls = (data.get("content_urls") or {}).get("desktop") or {}
    return {
        "title": data.get("title") or title,
        "description": data.get("description"),
        "extract": data.get("extract"),
        "url": content_urls.get("page"),
        "lat": coords.get("lat"),
        "lon": coords.get("lon"),
    }


def get_nearby_attractions(lat: float, lon: float, radius_m: int = 10000, limit: int = 12) -> list[dict]:
    """
    Find nearby points of interest via GeoSearch, then attach a short intro
    extract to each via a single batched `extracts` query.

    Returns a list of {title, lat, lon, distance_m, extract, url}. Silently
    returns [] on API hiccups rather than raising, so a flaky attractions
    lookup never blocks saving a destination.
    """
    radius_m = max(10, min(int(radius_m), 10000))  # geosearch caps at 10km
    limit = max(1, min(int(limit), 50))
    # Fetch a wider candidate pool (geosearch is distance-sorted and noisy near
    # city centers), then filter to real attractions and keep the closest `limit`.
    pool = min(max(limit * 5, 20), 100)

    try:
        geo = requests.get(
            _api_url(),
            params={
                "action": "query",
                "list": "geosearch",
                "gscoord": f"{lat}|{lon}",
                "gsradius": radius_m,
                "gslimit": pool,
                "format": "json",
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        geo.raise_for_status()
        hits = ((geo.json() or {}).get("query") or {}).get("geosearch") or []
    except Exception as exc:  # noqa: BLE001 - attractions are best-effort
        logger.warning("Wikimedia geosearch failed (%s): %s", type(exc).__name__, exc)
        return []

    hits = [h for h in hits if _is_attraction(h.get("title", ""))][:limit]
    if not hits:
        return []

    by_title = {
        h["title"]: {
            "title": h["title"],
            "lat": h.get("lat"),
            "lon": h.get("lon"),
            "distance_m": h.get("dist"),
            "extract": None,
            "url": f"https://{_LANG}.wikipedia.org/wiki/{quote(h['title'].replace(' ', '_'))}",
        }
        for h in hits
    }

    # One batched extracts call for intro descriptions of all nearby titles.
    try:
        ex = requests.get(
            _api_url(),
            params={
                "action": "query",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "titles": "|".join(by_title.keys()),
                "format": "json",
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        ex.raise_for_status()
        pages = ((ex.json() or {}).get("query") or {}).get("pages") or {}
        for page in pages.values():
            title = page.get("title")
            if title in by_title:
                by_title[title]["extract"] = page.get("extract")
    except Exception as exc:  # noqa: BLE001 - extracts are best-effort enrichment
        logger.warning("Wikimedia extracts failed (%s): %s", type(exc).__name__, exc)

    # Nearest first.
    return sorted(by_title.values(), key=lambda a: a["distance_m"] if a["distance_m"] is not None else 1e9)
