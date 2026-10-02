import logging
import re

import httpx
from pydantic import BaseModel
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .cache import TTLCache
from .logging_setup import log

logger = logging.getLogger(__name__)

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
WX_URL = "https://api.open-meteo.com/v1/forecast"

# Letters plus space . , ' - ; 1-60 chars. Blocks digits, URLs, control chars, prompt-ish payloads.
COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")  # ISO-3166-1 alpha-2
CITY_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .,'’\-]){0,59}$")

WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 71: "Light snow", 73: "Snow",
    75: "Heavy snow", 80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}


class InvalidCity(ValueError):
    pass


class CityNotFound(LookupError):
    pass


class WeatherUnavailable(RuntimeError):
    pass


class WeatherReport(BaseModel):
    city: str
    country: str
    condition: str
    temperature_c: float
    wind_kmh: float
    precipitation_mm: float
    rain_chance_pct: int | None

    def to_text(self) -> str:
        chance = "unknown" if self.rain_chance_pct is None else f"{self.rain_chance_pct}%"
        return (
            f"{self.city}, {self.country}: {self.condition}, {self.temperature_c}°C, "
            f"wind {self.wind_kmh} km/h, precipitation now {self.precipitation_mm} mm, "
            f"chance of rain today {chance}."
        )


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)  # timeouts, connection errors


class WeatherClient:
    """Async weather tool: validates input, caches, retries with backoff, never leaks raw errors."""

    def __init__(self, http: httpx.AsyncClient, cache: TTLCache[WeatherReport],
                 attempts: int = 3, backoff_max_s: float = 4.0):
        self._http, self._cache = http, cache
        self._attempts, self._backoff_max = attempts, backoff_max_s

    @staticmethod
    def validate_city(city: object) -> str:
        if not isinstance(city, str):
            raise InvalidCity("city must be a string")
        city = " ".join(city.split())
        if not CITY_RE.match(city):
            raise InvalidCity("city must be 1-60 letters (spaces, hyphens, apostrophes allowed)")
        return city

    @staticmethod
    def validate_country(code: object) -> str | None:
        if code is None or code == "":
            return None
        if not isinstance(code, str) or not COUNTRY_RE.match(code):
            raise InvalidCity("country_code must be a 2-letter ISO code")
        return code.upper()

    async def _get_json(self, url: str, params: dict) -> dict:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential_jitter(initial=0.3, max=self._backoff_max),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                resp = await self._http.get(url, params=params)
                resp.raise_for_status()
                return resp.json()

    async def get_weather(self, city: object, country_code: object = None) -> WeatherReport:
        city = self.validate_city(city)
        country = self.validate_country(country_code)
        key = f"{city.casefold()}|{country or ''}"
        if (hit := self._cache.get(key)) is not None:
            log(logger, "weather cache hit", city=key)
            return hit
        try:
            geo_params = {"name": city, "count": 1}
            if country:
                geo_params["countryCode"] = country  # disambiguates e.g. Hyderabad IN vs PK
            geo = await self._get_json(GEO_URL, geo_params)
            results = geo.get("results") or []
            if not results:
                raise CityNotFound(f"No city found matching '{city}'")
            place = results[0]
            wx = await self._get_json(WX_URL, {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
                "daily": "precipitation_probability_max",
                "forecast_days": 1,
            })
            cur = wx["current"]
            chances = wx.get("daily", {}).get("precipitation_probability_max") or [None]
            report = WeatherReport(
                city=place["name"],
                country=place.get("country", ""),
                condition=WMO.get(cur["weather_code"], "Unknown"),
                temperature_c=cur["temperature_2m"],
                wind_kmh=cur["wind_speed_10m"],
                precipitation_mm=cur["precipitation"],
                rain_chance_pct=chances[0],
            )
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("weather upstream failure: %s", type(exc).__name__)
            raise WeatherUnavailable("Weather service is temporarily unavailable") from exc
        self._cache.set(key, report)
        return report
