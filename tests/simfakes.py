"""Fakes for the simulator tests: a scripted mockllm model and a Simulator on a real environment."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.model import ChatMessage, GenerateConfig, Model, ModelOutput, get_model
from inspect_ai.tool import ToolChoice, ToolInfo
from inspect_ai.util import Store
from inspect_ai.util._store import init_subtask_store

import simrt.simulator as simulator_mod
from simrt.simulator import SimSettings, Simulator

ENVS = Path(__file__).resolve().parents[1] / "external" / "control-tower" / ".environments"
needs_envs = pytest.mark.skipif(not (ENVS / "web_scraping").exists(), reason="needs the pinned Control Tower envs")


@dataclass
class Call:
    input: list[ChatMessage]
    tools: list[ToolInfo]
    tool_choice: ToolChoice
    config: GenerateConfig

    @property
    def prompt(self) -> str:
        return self.input[-1].text


@dataclass
class ScriptedModel:
    """mockllm model that answers from a list and records every call it gets."""

    replies: list[str | ModelOutput]
    calls: list[Call] = field(default_factory=list)

    def model(self) -> Model:
        def generate(input: list[ChatMessage], tools: list[ToolInfo], tool_choice: ToolChoice, config: GenerateConfig) -> ModelOutput:
            self.calls.append(Call(list(input), list(tools), tool_choice, config))
            if not self.replies:
                raise AssertionError("unexpected model call")
            reply = self.replies.pop(0)
            return reply if isinstance(reply, ModelOutput) else ModelOutput.from_content("mockllm", reply)

        return get_model("mockllm/model", custom_outputs=generate, memoize=False)


def bash_reply(stdout: str = "", code: int = 0, stderr: str = "") -> str:
    return f"<exit_code>{code}</exit_code>\n<stdout>\n{stdout}\n</stdout>\n<stderr>\n{stderr}\n</stderr>"


def make_sim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    models: dict[str, ScriptedModel],
    env_id: str = "web_scraping",
    internet_simulator: dict[str, Any] | None = None,
    **settings: Any,
) -> Simulator:
    built = {name: m.model() for name, m in models.items()}
    monkeypatch.setattr(simulator_mod, "get_model", lambda name, **_: built[name])
    monkeypatch.setattr(simulator_mod, "load_reference", lambda *a, **k: "(reference transcript)")
    init_subtask_store(Store())
    s = SimSettings(
        model="main",
        envs_dir=str(ENVS),
        reference_dir=str(tmp_path / "ref"),
        trace_dir=str(tmp_path / "traces"),
        **settings,
    )
    return Simulator(
        settings=s,
        env_id=env_id,
        env_description="env description",
        main_task_id="mt",
        main_task_description="main task",
        internet_simulator=internet_simulator,
    )


def trace(sim: Simulator) -> list[dict[str, Any]]:
    if not sim.trace_path.exists():
        return []
    return [json.loads(line) for line in sim.trace_path.read_text().splitlines()]
