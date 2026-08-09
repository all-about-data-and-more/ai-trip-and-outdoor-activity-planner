"""
Open-Meteo Geocoding client — turn a destination name into coordinates.

Ported from the weather-mcp-server's weather_adapter.geocode: accepts either a
place name ("Kyoto", "Austin, TX", "Banff National Park") or a raw "lat,lon"
pair, and returns a normalized dict. No API key required.

    https://open-meteo.com/en/docs/geocoding-api
"""

import os
import re

import requests

_GEOCODE_URL = os.environ.get("WEATHER_GEOCODE_URL", "https://geocoding-api.open-meteo.com/v1/search")
_TIMEOUT = int(os.environ.get("WEATHER_HTTP_TIMEOUT", "10"))

# "lat,lon" e.g. "35.0116,135.7681"
_LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def geocode(location: str) -> dict:
    """
    Resolve a free-text location into coordinates.

    Returns {name, lat, lon, country, region, timezone}. Raises ValueError if a
    name can't be resolved, so callers can surface a clean "location not found"
    message instead of guessing.
    """
    location = (location or "").strip()
    if not location:
        raise ValueError("Location is required (e.g. 'Kyoto' or '35.01,135.77').")

    latlon = _LATLON_RE.match(location)
    if latlon:
        lat, lon = float(latlon.group(1)), float(latlon.group(2))
        return {
            "name": f"{lat},{lon}",
            "lat": lat,
            "lon": lon,
            "country": None,
            "region": None,
            "timezone": None,
        }

    resp = requests.get(
        _GEOCODE_URL,
        params={"name": location, "count": 1, "language": "en", "format": "json"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    if not results:
        raise ValueError(
            f"Could not find a location matching {location!r}. "
            "Try a 'City, Country' form or a 'lat,lon' pair."
        )
    top = results[0]
    return {
        "name": top.get("name") or location,
        "lat": top["latitude"],
        "lon": top["longitude"],
        "country": top.get("country"),
        "region": top.get("admin1"),
        "timezone": top.get("timezone"),
    }


def place_label(loc: dict) -> str:
    """Build a friendly display label like 'Kyoto, Kyoto, Japan'."""
    parts = [loc.get("name"), loc.get("region"), loc.get("country")]
    return ", ".join(str(p) for p in parts if p)
