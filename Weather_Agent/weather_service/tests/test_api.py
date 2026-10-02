import pytest
from fastapi.testclient import TestClient

from app.agent import AgentResult
from app.config import Settings
from app.main import create_app

HEAD = {"x-api-key": "k1"}


class StubAgent:
    def __init__(self, exc=None):
        self.exc = exc

    async def run(self, question):
        if self.exc:
            raise self.exc
        return AgentResult(answer=f"echo: {question}", steps=1, tool_calls=["get_weather"],
                           input_tokens=3, output_tokens=2)


def client(agent=None, **kw):
    settings = Settings(api_keys="k1,k2", **kw)
    return TestClient(create_app(settings, agent or StubAgent()), raise_server_exceptions=False)


def test_health_needs_no_auth():
    with client() as c:
        assert c.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "wrong"}])
def test_auth_required(headers):
    with client() as c:
        assert c.post("/ask", json={"question": "hi"}, headers=headers).status_code == 401


def test_ask_ok_and_request_id_header():
    with client() as c:
        r = c.post("/ask", json={"question": "Paris?"}, headers=HEAD)
        assert r.status_code == 200 and r.json()["answer"] == "echo: Paris?"
        assert r.headers["x-request-id"]


@pytest.mark.parametrize("body", [{}, {"question": ""}, {"question": "x" * 501}])
def test_input_validation(body):
    with client() as c:
        assert c.post("/ask", json=body, headers=HEAD).status_code == 422


def test_rate_limit_per_key():
    with client(rate_limit_per_min=2) as c:
        codes = [c.post("/ask", json={"question": "q"}, headers=HEAD).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert c.post("/ask", json={"question": "q"}, headers={"x-api-key": "k2"}).status_code == 200


def test_timeout_maps_to_504_and_bug_to_500_without_leak():
    with client(StubAgent(TimeoutError())) as c:
        assert c.post("/ask", json={"question": "q"}, headers=HEAD).status_code == 504
    with client(StubAgent(RuntimeError("secret internals"))) as c:
        r = c.post("/ask", json={"question": "q"}, headers=HEAD)
        assert r.status_code == 500 and "secret" not in r.text


class StubWeather:
    def __init__(self, exc=None):
        self.exc, self.args = exc, None

    async def get_weather(self, city, country_code=None):
        self.args = (city, country_code)
        if self.exc:
            raise self.exc
        from app.weather import WeatherReport
        return WeatherReport(city=city, country="X", condition="Clear sky", temperature_c=20.0,
                             wind_kmh=3.0, precipitation_mm=0.0, rain_chance_pct=10)


def wclient(weather):
    settings = Settings(api_keys="k1")
    return TestClient(create_app(settings, StubAgent(), weather), raise_server_exceptions=False)


def test_weather_endpoint_passes_country_and_requires_auth():
    w = StubWeather()
    with wclient(w) as c:
        assert c.get("/weather?city=Paris").status_code == 401
        r = c.get("/weather?city=Hyderabad&country_code=IN", headers=HEAD)
        assert r.status_code == 200 and r.json()["city"] == "Hyderabad" and w.args == ("Hyderabad", "IN")


@pytest.mark.parametrize("exc,status", [("invalid", 422), ("notfound", 404), ("down", 503)])
def test_weather_endpoint_error_mapping(exc, status):
    from app.weather import CityNotFound, InvalidCity, WeatherUnavailable
    err = {"invalid": InvalidCity("bad"), "notfound": CityNotFound("nf"), "down": WeatherUnavailable("down")}[exc]
    with wclient(StubWeather(err)) as c:
        assert c.get("/weather?city=X", headers=HEAD).status_code == status
