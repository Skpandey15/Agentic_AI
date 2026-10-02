import httpx
import pytest

from app.cache import TTLCache
from app.weather import CityNotFound, InvalidCity, WeatherClient, WeatherUnavailable

GEO_OK = {"results": [{"name": "Paris", "country": "France", "latitude": 48.8, "longitude": 2.3}]}
WX_OK = {
    "current": {"temperature_2m": 12.9, "precipitation": 0.0, "weather_code": 3, "wind_speed_10m": 1.4},
    "daily": {"precipitation_probability_max": [20]},
}


def make_client(handler, attempts=3):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WeatherClient(http, TTLCache(60, 10), attempts=attempts, backoff_max_s=0.01)


def ok_handler(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=GEO_OK if "geocoding" in request.url.host else WX_OK)
    return handler


async def test_success_and_text():
    report = await make_client(ok_handler([])).get_weather("Paris")
    assert report.condition == "Overcast" and report.rain_chance_pct == 20
    assert "Paris, France" in report.to_text()


async def test_cache_avoids_second_upstream_call():
    calls: list[str] = []
    client = make_client(ok_handler(calls))
    await client.get_weather("Paris")
    await client.get_weather("  paris ")  # normalized to the same key
    assert len(calls) == 2  # 1 geocode + 1 forecast, none for the second request


@pytest.mark.parametrize("bad", ["", "   ", "Paris123", "http://evil.com", "a" * 61, "x\x00y", "Paris; DROP", None, 42])
async def test_invalid_city_rejected_without_network(bad):
    def boom(request):
        raise AssertionError("network must not be called")
    with pytest.raises(InvalidCity):
        await make_client(boom).get_weather(bad)


async def test_city_not_found():
    client = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(CityNotFound):
        await client.get_weather("Nowhereville")


async def test_retries_503_then_succeeds():
    state = {"n": 0}

    def handler(request):
        if "geocoding" in request.url.host:
            state["n"] += 1
            if state["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, json=GEO_OK)
        return httpx.Response(200, json=WX_OK)

    report = await make_client(handler).get_weather("Paris")
    assert report.city == "Paris" and state["n"] == 3


async def test_gives_up_after_attempts_and_hides_details():
    client = make_client(lambda r: httpx.Response(500, text="secret internal stack trace"))
    with pytest.raises(WeatherUnavailable) as err:
        await client.get_weather("Paris")
    assert "secret" not in str(err.value)


async def test_no_retry_on_4xx():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(400)

    with pytest.raises(WeatherUnavailable):
        await make_client(handler).get_weather("Paris")
    assert state["n"] == 1


def test_cache_ttl_expiry():
    now = [0.0]
    cache = TTLCache(10, 2, clock=lambda: now[0])
    cache.set("a", 1)
    assert cache.get("a") == 1
    now[0] = 11
    assert cache.get("a") is None


async def test_country_code_sent_to_geocoder_and_cached_separately():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=GEO_OK if "geocoding" in request.url.host else WX_OK)

    client = make_client(handler)
    await client.get_weather("Paris", "fr")
    assert seen[0]["countryCode"] == "FR"
    n = len(seen)
    await client.get_weather("Paris")  # different cache key (no country)
    assert len(seen) > n


@pytest.mark.parametrize("bad", ["FRA", "1", "F!", 5])
async def test_invalid_country_code(bad):
    with pytest.raises(InvalidCity):
        await make_client(lambda r: httpx.Response(200, json={})).get_weather("Paris", bad)
