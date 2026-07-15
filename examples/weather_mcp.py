"""
examples/weather_mcp.py — a tiny, self-contained weather MCP server.

Used as the reference example for selmakit's MCP client (Phase 1). It speaks
MCP over stdio and exposes a single `get_weather` tool backed by the free
Open-Meteo API (no API key, worldwide). Wire it into selmakit.json as:

    "mcp": {
      "enabled": true,
      "servers": {
        "weather": { "command": "uv", "args": ["run", "examples/weather_mcp.py"] }
      }
    }

Run standalone for a quick smoke test:

    uv run examples/weather_mcp.py        # serves on stdio (blocks)
"""
from __future__ import annotations

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")

_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo WMO weather-code → human description (abridged).
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog", 51: "light drizzle", 53: "drizzle",
    55: "dense drizzle", 61: "slight rain", 63: "rain", 65: "heavy rain",
    71: "slight snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


@mcp.tool()
async def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: City name, e.g. "Berlin" or "New York".
    """
    async with httpx.AsyncClient(timeout=15) as client:
        geo = await client.get(_GEOCODE, params={"name": city, "count": 1})
        geo.raise_for_status()
        results = geo.json().get("results")
        if not results:
            return f"Could not find a location named {city!r}."
        loc = results[0]
        lat, lon = loc["latitude"], loc["longitude"]
        label = ", ".join(x for x in (loc.get("name"), loc.get("country")) if x)

        fc = await client.get(_FORECAST, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        })
        fc.raise_for_status()
        cur = fc.json()["current"]

    desc = _WMO.get(cur["weather_code"], f"code {cur['weather_code']}")
    return (
        f"Weather in {label}: {desc}, {cur['temperature_2m']}°C "
        f"(feels like {cur['apparent_temperature']}°C), "
        f"wind {cur['wind_speed_10m']} km/h."
    )


if __name__ == "__main__":
    mcp.run()
