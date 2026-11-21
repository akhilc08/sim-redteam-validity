"""`rotate/<model>,<model>,...` — spread calls over several equivalent model routes.

Bedrock enforces hidden per-route daily token caps on newer accounts. The same Claude model is
reachable via several inference profiles (us./global.), each with its own cap. This provider is
sticky (uses the first ready route in list order, which keeps prompt caches warm) and fails over
to the next route when one fails with a retryable error (throttling, overload, 5xx, connection or
timeout errors), cooling that route down, so a long run keeps going instead of stalling. A
non-retryable error (e.g. a bad request) is raised at once and not retried. When every route is
cooling down, the error propagates to Inspect's normal retry/backoff (via should_retry).

Errors are classified by exception type, HTTP status and AWS error code, never by message text alone:
Inspect wraps a provider-returned error in a RuntimeError carrying the whole request, so matching on
text would treat any prompt that mentions "per day" or "throttl" as a throttle and retry a bad request.
Every output is tagged with the route that served it (output.metadata["rotate_route"], kept in the eval
log's model events) and token usage is tallied per route in `route_usage`; Inspect's own usage stats
record all routes under the one rotate/... name.
"""

import logging
import os
import time
from typing import Any, Literal

from inspect_ai.model import ChatMessage, GenerateConfig, Model, ModelAPI, ModelOutput, ModelUsage, get_model, modelapi
from inspect_ai.model._model_call import ModelCall
from inspect_ai.tool import ToolChoice, ToolInfo

logger = logging.getLogger(__name__)

DAILY_CAP_COOLDOWN_S = 20 * 60
RATE_LIMIT_COOLDOWN_S = 2 * 60
TRANSIENT_COOLDOWN_S = 30  # other retryable failures (overload, 5xx, connection, timeout)
THROTTLE_CODES = {"ThrottlingException", "Throttling", "TooManyRequestsException", "RequestThrottled",
                  "RequestLimitExceeded"}

# Shared across all instances in the process: a throttled route is throttled for everyone.
_cooldown_until: dict[str, float] = {}
route_usage: dict[str, ModelUsage] = {}


def _aws_error_code(ex: BaseException) -> str | None:
    resp = getattr(ex, "response", None)  # botocore ClientError; httpx responses are not dicts
    return resp.get("Error", {}).get("Code") if isinstance(resp, dict) else None


def classify_throttle(ex: BaseException) -> float | None:
    """Cooldown seconds if `ex` is a provider throttle (HTTP 429 or an AWS throttling code), else None."""
    if getattr(ex, "status_code", None) != 429 and _aws_error_code(ex) not in THROTTLE_CODES:
        return None
    return DAILY_CAP_COOLDOWN_S if "per day" in str(ex).lower() else RATE_LIMIT_COOLDOWN_S


def split_route(route: str) -> tuple[str, str | None]:
    """`model@region` -> (model, region). The region pins a Bedrock route (e.g. eu. profiles)."""
    model, sep, region = route.rpartition("@")
    return (model, region) if sep and model and region else (route, None)


def _build_route(route: str, config: GenerateConfig) -> Model:
    """Create a route's model; Bedrock clients read their region from the env at construction."""
    model, region = split_route(route)
    if region is None:
        return get_model(model, config=config)
    saved = {k: os.environ.get(k) for k in ("AWS_REGION", "AWS_DEFAULT_REGION")}
    try:
        os.environ.pop("AWS_REGION", None)
        os.environ["AWS_DEFAULT_REGION"] = region
        return get_model(model, config=config, memoize=False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _tag(route: str, result: ModelOutput | tuple[ModelOutput | Exception, ModelCall]) -> Any:
    """Record which route served the call, and its usage under that route."""
    output = result[0] if isinstance(result, tuple) else result
    if isinstance(output, ModelOutput):
        output.metadata = {**(output.metadata or {}), "rotate_route": route}
        if output.usage:
            route_usage[route] = route_usage[route] + output.usage if route in route_usage else output.usage
        logger.debug("rotate: served by %s", route)
    return result


class RotatingModelAPI(ModelAPI):
    def __init__(
        self,
        model_name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        config: GenerateConfig = GenerateConfig(),
        **model_args: Any,
    ) -> None:
        super().__init__(model_name=model_name, base_url=base_url, api_key=api_key, config=config)
        self.route_names = [n.strip() for n in model_name.split(",") if n.strip()]
        if not self.route_names:
            raise ValueError("rotate/ needs at least one model, e.g. rotate/a,b")
        self.routes: list[Model] = [_build_route(n, config) for n in self.route_names]

    @property
    def primary(self) -> ModelAPI:
        return self.routes[0].api

    def _order(self) -> list[int]:
        """Ready routes in preference order; if none are ready, the one that recovers soonest."""
        now = time.time()
        idx = list(range(len(self.routes)))
        ready = [i for i in idx if _cooldown_until.get(self.route_names[i], 0) <= now]
        return ready or sorted(idx, key=lambda i: _cooldown_until.get(self.route_names[i], 0))[:1]

    @staticmethod
    def _retryable(api: ModelAPI, ex: Exception) -> bool:
        return classify_throttle(ex) is not None or api.should_retry(ex)

    async def generate(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput | tuple[ModelOutput | Exception, ModelCall]:
        last: Exception | None = None
        for i in self._order():
            name, api = self.route_names[i], self.routes[i].api
            try:
                result = await api.generate(input, tools, tool_choice, config)
            except Exception as ex:  # noqa: BLE001 - fail over on retryable errors, raise the rest
                if not self._retryable(api, ex):
                    raise
                cooldown = classify_throttle(ex) or TRANSIENT_COOLDOWN_S
                _cooldown_until[name] = time.time() + cooldown
                logger.warning("rotate: %s failed (%ss cooldown): %s", name, cooldown, str(ex)[:120])
                last = ex
                continue
            return _tag(name, result)
        assert last is not None
        raise last

    # ---- delegate provider behaviour to the routes / the primary route -------------------
    def should_retry(self, ex: Exception) -> bool:
        return any(self._retryable(r.api, ex) for r in self.routes)

    def max_tokens(self) -> int | None:
        return self.primary.max_tokens()

    def max_tokens_for_config(self, config: GenerateConfig) -> int | None:
        return self.primary.max_tokens_for_config(config)

    def max_connections(self) -> int:
        return self.primary.max_connections()

    def connection_key(self) -> str:
        return "rotate:" + ",".join(self.route_names)

    def collapse_user_messages(self) -> bool:
        return self.primary.collapse_user_messages()

    def collapse_assistant_messages(self) -> bool:
        return self.primary.collapse_assistant_messages()

    def tools_required(self) -> bool:
        return self.primary.tools_required()

    def tool_result_images(self) -> bool:
        return self.primary.tool_result_images()

    def force_reasoning_history(self) -> Literal["none", "all", "last"] | None:
        return self.primary.force_reasoning_history()


# Registered separately: @modelapi replaces the class with a factory function.
rotate_modelapi = modelapi(name="rotate")(RotatingModelAPI)
