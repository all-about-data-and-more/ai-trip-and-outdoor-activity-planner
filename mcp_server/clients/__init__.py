"""Key-less third-party API clients for the Trip Planner.

Each module owns ALL of its HTTP calls + response parsing and returns clean,
JSON-serializable dicts (the "adapter" pattern from the weather-mcp-server's
weather_adapter.py). Callers — planner_tools, the app's read endpoints, and the
MCP tools — never issue raw ``requests`` calls themselves.

  * geocoding_client  — Open-Meteo Geocoding (name -> lat/lon)
  * weather_client    — Open-Meteo Forecast (daily forecast + current)
  * air_quality_client— Open-Meteo Air Quality (AQI, PM, UV, pollen)
  * wikimedia_client  — Wikimedia REST summary + GeoSearch attractions

None require an API key for non-commercial use.
"""
