import hmac
import logging
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import anthropic
import httpx
from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agent import WeatherAgent
from .cache import TTLCache
from .config import Settings, get_settings
from .logging_setup import log, request_id_var, setup_logging
from .weather import CityNotFound, InvalidCity, WeatherClient, WeatherReport, WeatherUnavailable

logger = logging.getLogger("app")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class AskResponse(BaseModel):
    answer: str
    steps: int
    tool_calls: list[str]
    input_tokens: int
    output_tokens: int
    truncated: bool


class RateLimiter:
    """Per-key sliding window, in-process. Use Redis if you run more than one instance."""

    def __init__(self, per_min: int, clock=time.monotonic):
        self._per_min, self._clock = per_min, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = self._clock(), self._hits[key]
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= self._per_min:
            return False
        q.append(now)
        return True


def create_app(settings: Settings | None = None, agent=None, weather=None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = llm = None
        if agent is None:  # tests inject a fake agent and skip real clients
            http = httpx.AsyncClient(
                timeout=settings.weather_timeout_s,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            )
            llm = AsyncAnthropic(max_retries=settings.llm_max_retries)
            cache = TTLCache(settings.cache_ttl_s, settings.cache_max_entries)
            app.state.weather = WeatherClient(http, cache, settings.weather_attempts)
            app.state.agent = WeatherAgent(llm, app.state.weather, settings)
        else:
            app.state.agent, app.state.weather = agent, weather
        app.state.limiter = RateLimiter(settings.rate_limit_per_min)
        yield
        if http:
            await http.aclose()
        if llm:
            await llm.close()

    app = FastAPI(title="Weather Agent", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request_id_var.set(rid)
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        log(logger, "request", method=request.method, path=request.url.path,
            status=response.status_code, ms=round((time.perf_counter() - start) * 1000))
        return response

    async def authenticate(request: Request) -> str:
        supplied = request.headers.get("x-api-key", "")
        for valid in settings.api_key_set:
            if hmac.compare_digest(supplied.encode(), valid.encode()):  # constant-time compare
                return valid
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/weather", response_model=WeatherReport)
    async def weather_endpoint(city: str, request: Request, country_code: str | None = None,
                      key: str = Depends(authenticate)):
        """Direct tool access for UIs: no LLM call, so it is fast and nearly free."""
        if not request.app.state.limiter.allow(key):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        try:
            return await request.app.state.weather.get_weather(city, country_code)
        except InvalidCity as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except CityNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except WeatherUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    @app.post("/ask", response_model=AskResponse)
    async def ask(body: AskRequest, request: Request, key: str = Depends(authenticate)):
        if not request.app.state.limiter.allow(key):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        try:
            result = await request.app.state.agent.run(body.question)
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Request timed out")
        except anthropic.APIError:
            logger.exception("LLM provider error")
            raise HTTPException(status_code=502, detail="Upstream AI service error")
        log(logger, "agent done", steps=result.steps, tools=result.tool_calls,
            in_tokens=result.input_tokens, out_tokens=result.output_tokens)
        return AskResponse(**result.__dict__)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error")
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    return app


app = create_app()
