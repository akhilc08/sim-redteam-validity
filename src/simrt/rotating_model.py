"""`rotate/<model>,<model>,...` — spread calls over several equivalent model routes.

Bedrock enforces hidden per-route daily token caps on newer accounts. The same Claude model is
reachable via several inference profiles (us./global.), each with its own cap. This provider is
sticky (uses the first ready route in list order, which keeps prompt caches warm) and fails over
to the next route when one is throttled, cooling the throttled route down, so a long run keeps
going instead of stalling. When every route is cooling down, the error propagates to Inspect's
normal retry/backoff (via should_retry).
"""

import logging
import os
import time
from typing import Any, Literal

from inspect_ai.model import ChatMessage, GenerateConfig, Model, ModelAPI, ModelOutput, get_model, modelapi
from inspect_ai.model._model_call import ModelCall
from inspect_ai.tool import ToolChoice, ToolInfo

logger = logging.getLogger(__name__)

DAILY_CAP_COOLDOWN_S = 20 * 60
RATE_LIMIT_COOLDOWN_S = 2 * 60

# Shared across all instances in the process: a throttled route is throttled for everyone.
_cooldown_until: dict[str, float] = {}


def classify_throttle(ex: BaseException) -> float | None:
    """Cooldown seconds if `ex` is a throttle we should rotate away from, else None."""
    text = f"{type(ex).__name__} {ex}".lower()
    if "per day" in text:
        return DAILY_CAP_COOLDOWN_S
    status = getattr(ex, "status_code", None)
    if status == 429 or "ratelimit" in text or "throttl" in text or "too many tokens" in text:
        return RATE_LIMIT_COOLDOWN_S
    return None


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

    async def generate(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput | tuple[ModelOutput | Exception, ModelCall]:
        last: BaseException | None = None
        for i in self._order():
            name = self.route_names[i]
            try:
                return await self.routes[i].api.generate(input, tools, tool_choice, config)
            except Exception as ex:  # noqa: BLE001 - classify, then rotate or re-raise
                cooldown = classify_throttle(ex)
                if cooldown is None:
                    raise
                _cooldown_until[name] = time.time() + cooldown
                logger.warning("rotate: %s throttled (%ss cooldown): %s", name, cooldown, str(ex)[:120])
                last = ex
        assert last is not None
        raise last

    # ---- delegate provider behaviour to the primary route ------------------------------
    def should_retry(self, ex: Exception) -> bool:
        return classify_throttle(ex) is not None or self.primary.should_retry(ex)

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
