"""
Open-Meteo Weather (Forecast) client.

Ported/trimmed from the weather-mcp-server's weather_adapter: resolves a
location via geocoding_client, then fetches a daily forecast (and current
conditions). Returns clean dicts with a human-readable `conditions` phrase
translated from Open-Meteo's numeric WMO weather codes. No API key required.

    https://open-meteo.com/en/docs
"""

import os

import requests

from . import geocoding_client

_FORECAST_URL = os.environ.get("WEATHER_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
_TIMEOUT = int(os.environ.get("WEATHER_HTTP_TIMEOUT", "10"))

TEMPERATURE_UNIT = os.environ.get("WEATHER_TEMPERATURE_UNIT", "fahrenheit")
WIND_SPEED_UNIT = os.environ.get("WEATHER_WIND_SPEED_UNIT", "mph")
PRECIPITATION_UNIT = os.environ.get("WEATHER_PRECIPITATION_UNIT", "inch")

_MAX_FORECAST_DAYS = 16

# Open-Meteo returns numeric WMO weather-interpretation codes, not text.
_WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow fall", 73: "Moderate snow fall", 75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def describe_weather_code(code) -> str:
    """Translate an Open-Meteo WMO weather code into a human-readable phrase."""
    try:
        return _WMO_CODES.get(int(code), f"Unknown conditions (code {code})")
    except (TypeError, ValueError):
        return "Unknown conditions"


def _at(seq, i):
    """Safely index a parallel array Open-Meteo returns (tolerates None/short)."""
    if not seq or i >= len(seq):
        return None
    return seq[i]


def _units() -> dict:
    return {
        "temperature": TEMPERATURE_UNIT,
        "wind_speed": WIND_SPEED_UNIT,
        "precipitation": PRECIPITATION_UNIT,
    }


def get_current(location: str) -> dict:
    """Fetch current conditions for `location`."""
    loc = geocoding_client.geocode(location)
    resp = requests.get(
        _FORECAST_URL,
        params={
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "precipitation,weather_code,wind_speed_10m",
            "temperature_unit": TEMPERATURE_UNIT,
            "wind_speed_unit": WIND_SPEED_UNIT,
            "precipitation_unit": PRECIPITATION_UNIT,
            "timezone": "auto",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    cur = (resp.json() or {}).get("current", {}) or {}
    return {
        "location": geocoding_client.place_label(loc),
        "lat": loc["lat"],
        "lon": loc["lon"],
        "as_of": cur.get("time"),
        "temperature": cur.get("temperature_2m"),
        "feels_like": cur.get("apparent_temperature"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "precipitation": cur.get("precipitation"),
        "wind_speed": cur.get("wind_speed_10m"),
        "conditions": describe_weather_code(cur.get("weather_code")),
        "units": _units(),
    }


def get_forecast(location: str, days: int = 7) -> dict:
    """
    Fetch a multi-day daily forecast for `location`. `days` is clamped to 1..16.

    Each day: date, temp_high, temp_low, precip_chance_pct, precip_amount,
    wind_max, and a human-readable conditions phrase.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, _MAX_FORECAST_DAYS))

    loc = geocoding_client.geocode(location)
    resp = requests.get(
        _FORECAST_URL,
        params={
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
            "temperature_unit": TEMPERATURE_UNIT,
            "wind_speed_unit": WIND_SPEED_UNIT,
            "precipitation_unit": PRECIPITATION_UNIT,
            "timezone": "auto",
            "forecast_days": days,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    daily = (resp.json() or {}).get("daily", {}) or {}
    dates = daily.get("time", []) or []

    out_days = [
        {
            "date": d,
            "temp_high": _at(daily.get("temperature_2m_max"), i),
            "temp_low": _at(daily.get("temperature_2m_min"), i),
            "precip_chance_pct": _at(daily.get("precipitation_probability_max"), i),
            "precip_amount": _at(daily.get("precipitation_sum"), i),
            "wind_max": _at(daily.get("wind_speed_10m_max"), i),
            "conditions": describe_weather_code(_at(daily.get("weather_code"), i)),
        }
        for i, d in enumerate(dates)
    ]

    return {
        "location": geocoding_client.place_label(loc),
        "lat": loc["lat"],
        "lon": loc["lon"],
        "days": out_days,
        "units": _units(),
    }
