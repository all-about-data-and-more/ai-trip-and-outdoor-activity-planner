"""
Open-Meteo Air Quality client — AQI, particulate matter, UV, and pollen.

The air-quality API returns HOURLY series; this client aggregates them to a
per-day summary (daily max of each variable) so the values line up with the
daily weather forecast and can be stored one row per destination per day in
`weather_snapshots`. No API key required.

    https://open-meteo.com/en/docs/air-quality-api

Notes:
  * `us_aqi` is the US EPA Air Quality Index (0-500; >100 = unhealthy for
    sensitive groups). That's the value the reschedule logic thresholds on.
  * Pollen variables are only populated for European locations on Open-Meteo's
    side; elsewhere they come back null, which this client passes through.
"""

import os

import requests

from . import geocoding_client

_AIR_QUALITY_URL = os.environ.get(
    "AIR_QUALITY_URL", "https://air-quality-api.open-meteo.com/v1/air-quality"
)
_TIMEOUT = int(os.environ.get("WEATHER_HTTP_TIMEOUT", "10"))

_POLLEN_VARS = [
    "alder_pollen", "birch_pollen", "grass_pollen",
    "mugwort_pollen", "olive_pollen", "ragweed_pollen",
]
_HOURLY_VARS = ["us_aqi", "pm2_5", "pm10", "uv_index"] + _POLLEN_VARS


def _daily_max(times: list[str], values: list) -> dict[str, float]:
    """Collapse an hourly series into {date: max_value_for_that_date}, skipping
    nulls. `times` are ISO strings like '2026-08-09T14:00'."""
    out: dict[str, float] = {}
    for t, v in zip(times or [], values or []):
        if v is None:
            continue
        day = t[:10]
        out[day] = v if day not in out else max(out[day], v)
    return out


def get_air_quality(location: str, days: int = 7) -> dict:
    """
    Fetch an air-quality forecast for `location`, summarized per day.

    Returns {location, lat, lon, days: [{date, aqi, pm2_5, pm10, uv_index_max,
    pollen: {...}}], units}. `days` is clamped to 1..7 (the air-quality
    forecast horizon).
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 7))

    loc = geocoding_client.geocode(location)
    resp = requests.get(
        _AIR_QUALITY_URL,
        params={
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "hourly": ",".join(_HOURLY_VARS),
            "timezone": "auto",
            "forecast_days": days,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    hourly = (resp.json() or {}).get("hourly", {}) or {}
    times = hourly.get("time", []) or []

    aqi_by_day = _daily_max(times, hourly.get("us_aqi"))
    pm25_by_day = _daily_max(times, hourly.get("pm2_5"))
    pm10_by_day = _daily_max(times, hourly.get("pm10"))
    uv_by_day = _daily_max(times, hourly.get("uv_index"))
    pollen_by_day = {
        var: _daily_max(times, hourly.get(var)) for var in _POLLEN_VARS
    }

    # Union of all dates that appeared, in order.
    all_dates = sorted({t[:10] for t in times})
    out_days = []
    for day in all_dates:
        pollen = {
            var: pollen_by_day[var].get(day)
            for var in _POLLEN_VARS
            if pollen_by_day[var].get(day) is not None
        }
        out_days.append(
            {
                "date": day,
                "aqi": aqi_by_day.get(day),
                "pm2_5": pm25_by_day.get(day),
                "pm10": pm10_by_day.get(day),
                "uv_index_max": uv_by_day.get(day),
                "pollen": pollen or None,
            }
        )

    return {
        "location": geocoding_client.place_label(loc),
        "lat": loc["lat"],
        "lon": loc["lon"],
        "days": out_days,
        "units": {"aqi": "us_aqi", "pm": "µg/m³", "uv_index": "index"},
    }
