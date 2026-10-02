import asyncio
from types import SimpleNamespace as NS

import pytest

from app.agent import WeatherAgent
from app.config import Settings
from app.weather import CityNotFound, WeatherReport, WeatherUnavailable


def text_reply(text):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)],
              usage=NS(input_tokens=10, output_tokens=5))


def tool_reply(*cities, tool="get_weather"):
    blocks = [NS(type="tool_use", id=f"t{i}", name=tool, input={"city": c}) for i, c in enumerate(cities)]
    return NS(stop_reason="tool_use", content=blocks, usage=NS(input_tokens=10, output_tokens=5))


class FakeLLM:
    def __init__(self, replies, delay=0.0):
        self.replies, self.delay, self.calls = list(replies), delay, []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay)
        return self.replies.pop(0) if self.replies else tool_reply("Paris")  # loops forever if allowed


class FakeWeather:
    def __init__(self, behavior=None):
        self.behavior = behavior

    async def get_weather(self, city):
        if self.behavior:
            raise self.behavior
        return WeatherReport(city=city, country="X", condition="Clear sky", temperature_c=20.0,
                             wind_kmh=3.0, precipitation_mm=0.0, rain_chance_pct=10)


def agent(llm, weather=None, **overrides):
    return WeatherAgent(llm, weather or FakeWeather(), Settings(**overrides))


async def test_tool_then_answer_and_token_accounting():
    llm = FakeLLM([tool_reply("Paris"), text_reply("No umbrella needed.")])
    res = await agent(llm).run("Umbrella in Paris?")
    assert res.answer == "No umbrella needed." and res.tool_calls == ["get_weather"]
    assert res.steps == 2 and res.input_tokens == 20 and res.output_tokens == 10
    assert llm.calls[0]["system"] and llm.calls[0]["tools"]


async def test_no_tool_needed():
    res = await agent(FakeLLM([text_reply("4")])).run("2+2?")
    assert res.tool_calls == [] and res.steps == 1


async def test_parallel_tool_calls_all_answered():
    llm = FakeLLM([tool_reply("Paris", "Tokyo"), text_reply("Both fine.")])
    await agent(llm).run("Compare")
    results = llm.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t0", "t1"]


@pytest.mark.parametrize("exc", [CityNotFound("nope"), WeatherUnavailable("down"), RuntimeError("bug")])
async def test_tool_errors_are_returned_to_model_not_raised(exc):
    llm = FakeLLM([tool_reply("Paris"), text_reply("Sorry, cannot check right now.")])
    res = await agent(llm, FakeWeather(exc)).run("Paris?")
    sent = llm.calls[1]["messages"][-1]["content"][0]
    assert sent["is_error"] is True and res.answer.startswith("Sorry")


async def test_unknown_tool_flagged_as_error():
    llm = FakeLLM([tool_reply("Paris", tool="delete_everything"), text_reply("ok")])
    await agent(llm).run("x")
    assert llm.calls[1]["messages"][-1]["content"][0]["is_error"] is True


async def test_step_limit_stops_runaway_loop():
    llm = FakeLLM([])  # always asks for a tool
    res = await agent(llm, max_steps=3).run("loop")
    assert res.truncated and len(llm.calls) == 3


async def test_deadline_raises_timeout():
    with pytest.raises(TimeoutError):
        await agent(FakeLLM([text_reply("slow")], delay=1.0), request_deadline_s=0.05).run("q")
