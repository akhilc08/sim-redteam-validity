import time
from dataclasses import dataclass, field

import pytest
from inspect_ai.model import GenerateConfig, ModelOutput, ModelUsage

import simrt.rotating_model as rm


class Throttle(Exception):
    status_code = 429


class Overloaded(Exception):
    status_code = 529


class BadRequest(Exception):
    status_code = 400


class AwsThrottle(Exception):
    """Shape of botocore's ClientError for a Bedrock throttle."""

    response = {"Error": {"Code": "ThrottlingException", "Message": "Too many tokens per day"}}


@dataclass
class FakeAPI:
    name: str
    fail_with: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def generate(self, input, tools, tool_choice, config):  # noqa: ANN001
        self.calls.append(self.name)
        if self.fail_with:
            raise self.fail_with
        out = ModelOutput.from_content(model=self.name, content=f"from {self.name}")
        out.usage = ModelUsage(input_tokens=10, output_tokens=2, total_tokens=12)
        return out

    def should_retry(self, ex: Exception) -> bool:  # like Inspect providers: by HTTP status only
        return getattr(ex, "status_code", None) in (408, 429, 500, 503, 529)


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
    rm.route_usage.clear()


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
    a = FakeAPI("a", fail_with=Throttle("ThrottlingException: Too many tokens per day"))
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


async def test_non_retryable_error_is_raised_without_failover() -> None:
    a, b = FakeAPI("a", fail_with=BadRequest("bad request")), FakeAPI("b")
    r = make([a, b])
    with pytest.raises(BadRequest):
        await gen(r)
    assert b.calls == [] and not r.should_retry(BadRequest("bad request"))


async def test_fails_over_on_any_retryable_error() -> None:
    """An overloaded/5xx route is skipped like a throttled one (the old code re-raised at once)."""
    a, b, c = FakeAPI("a", fail_with=Overloaded("overloaded")), FakeAPI("b", fail_with=Throttle("slow down")), FakeAPI("c")
    r = make([a, b, c])
    assert await gen(r) == "from c"
    assert rm._cooldown_until["a"] > time.time() and rm._cooldown_until["b"] > time.time()
    assert r.should_retry(Overloaded("overloaded"))


async def test_non_retryable_error_on_a_later_route_is_raised() -> None:
    a, b, c = FakeAPI("a", fail_with=Throttle("429")), FakeAPI("b", fail_with=BadRequest("400")), FakeAPI("c")
    with pytest.raises(BadRequest):
        await gen(make([a, b, c]))
    assert c.calls == []


def test_request_text_in_a_wrapped_error_is_not_a_throttle() -> None:
    """Inspect wraps a provider-returned error in a RuntimeError carrying the request; a prompt that
    mentions 'per day' or 'throttling' must not make that bad request retryable."""
    wrapped = RuntimeError("BadRequestError('invalid')\n\nRequest:\n{\"messages\": \"Too many tokens per day; throttling\"}")
    r = make([FakeAPI("a"), FakeAPI("b")])
    assert rm.classify_throttle(wrapped) is None
    assert not r.should_retry(wrapped)


async def test_usage_and_route_are_kept_per_route() -> None:
    a, b = FakeAPI("a", fail_with=Throttle("429")), FakeAPI("b")
    r = make([a, b])
    out = await r.generate([], [], "auto", GenerateConfig())
    assert isinstance(out, ModelOutput) and out.metadata == {"rotate_route": "b"}
    await gen(r)
    assert set(rm.route_usage) == {"b"} and rm.route_usage["b"].input_tokens == 20


def test_classify() -> None:
    assert rm.classify_throttle(Throttle("Too many tokens per day")) == rm.DAILY_CAP_COOLDOWN_S
    assert rm.classify_throttle(Throttle("x")) == rm.RATE_LIMIT_COOLDOWN_S
    assert rm.classify_throttle(AwsThrottle("An error occurred (ThrottlingException): Too many tokens per day")) \
        == rm.DAILY_CAP_COOLDOWN_S
    assert rm.classify_throttle(Exception("Too many tokens per day")) is None  # text alone is not enough
    assert rm.classify_throttle(ValueError("nope")) is None


def test_split_route() -> None:
    assert rm.split_route("anthropic/bedrock/eu.anthropic.x@eu-west-1") == ("anthropic/bedrock/eu.anthropic.x", "eu-west-1")
    assert rm.split_route("anthropic/bedrock/us.anthropic.x") == ("anthropic/bedrock/us.anthropic.x", None)
