import time
from dataclasses import dataclass, field

import pytest
from inspect_ai.model import GenerateConfig, ModelOutput

import simrt.rotating_model as rm


class Throttle(Exception):
    status_code = 429


@dataclass
class FakeAPI:
    name: str
    fail_with: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def generate(self, input, tools, tool_choice, config):  # noqa: ANN001
        self.calls.append(self.name)
        if self.fail_with:
            raise self.fail_with
        return ModelOutput.from_content(model=self.name, content=f"from {self.name}")


@dataclass
class FakeModel:
    api: FakeAPI


def make(apis: list[FakeAPI]) -> rm.RotatingModelAPI:
    r = object.__new__(rm.RotatingModelAPI)
    r.route_names = [a.name for a in apis]
    r.routes = [FakeModel(a) for a in apis]  # type: ignore[misc]
    return r


@pytest.fixture(autouse=True)
def clear_cooldowns() -> None:
    rm._cooldown_until.clear()


async def gen(r: rm.RotatingModelAPI) -> str:
    out = await r.generate([], [], "auto", GenerateConfig())
    assert isinstance(out, ModelOutput)
    return out.completion


async def test_sticky_to_first_ready_route() -> None:
    """Sticky routing keeps prompt caches warm on one route."""
    a, b = FakeAPI("a"), FakeAPI("b")
    r = make([a, b])
    assert [await gen(r) for _ in range(3)] == ["from a"] * 3 and b.calls == []


async def test_recovers_to_primary_after_cooldown() -> None:
    a, b = FakeAPI("a"), FakeAPI("b")
    r = make([a, b])
    rm._cooldown_until["a"] = time.time() - 1  # cooldown expired
    assert await gen(r) == "from a"


async def test_daily_cap_rotates_and_cools_down() -> None:
    a = FakeAPI("a", fail_with=Exception("ThrottlingException: Too many tokens per day"))
    b = FakeAPI("b")
    r = make([a, b])
    assert await gen(r) == "from b"
    assert rm._cooldown_until["a"] > time.time() + rm.DAILY_CAP_COOLDOWN_S - 5
    a.calls.clear()
    for _ in range(3):
        assert await gen(r) == "from b"
    assert a.calls == []  # cooled-down route is skipped


async def test_all_throttled_raises_for_outer_retry() -> None:
    r = make([FakeAPI("a", fail_with=Throttle("429")), FakeAPI("b", fail_with=Throttle("429"))])
    with pytest.raises(Throttle):
        await gen(r)
    assert r.should_retry(Throttle("429"))


async def test_non_throttle_error_is_not_swallowed() -> None:
    a, b = FakeAPI("a", fail_with=ValueError("bad request")), FakeAPI("b")
    r = make([a, b])
    with pytest.raises(ValueError):
        await gen(r)
    assert b.calls == []


def test_classify() -> None:
    assert rm.classify_throttle(Exception("Too many tokens per day")) == rm.DAILY_CAP_COOLDOWN_S
    assert rm.classify_throttle(Throttle("x")) == rm.RATE_LIMIT_COOLDOWN_S
    assert rm.classify_throttle(ValueError("nope")) is None


def test_split_route() -> None:
    assert rm.split_route("anthropic/bedrock/eu.anthropic.x@eu-west-1") == ("anthropic/bedrock/eu.anthropic.x", "eu-west-1")
    assert rm.split_route("anthropic/bedrock/us.anthropic.x") == ("anthropic/bedrock/us.anthropic.x", None)
