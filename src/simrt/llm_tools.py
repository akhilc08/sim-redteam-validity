"""Minimal tool loop: let a model consult the codebase snapshot before giving a final answer.

These calls run inside an agent's sample but are not part of the agent's conversation, so they are
exempt from the sample's message limit: Inspect checks the innermost active `message_limit` on
every `Model.generate` (inspect_ai/model/_model.py, util/_limit.py check_message_limit), and a
nested `message_limit(None)` scope is the supported way to lift it.
"""

import json
import logging
from dataclasses import dataclass, field

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo, ToolParam, ToolParams
from inspect_ai.util import message_limit

from simrt.codebase import CodebaseSnapshot

logger = logging.getLogger(__name__)
FINAL_ANSWER_NOTE = "Lookup budget exhausted. Give your final answer now, without tool calls."


def _tools(dir_path: str, file_path: str, grep_path: str) -> list[ToolInfo]:
    return [
        ToolInfo(
            name="list_dir",
            description="List files under a directory of the codebase snapshot (relative path, depth<=3).",
            parameters=ToolParams(
                properties={
                    "path": ToolParam(type="string", description=dir_path),
                    "depth": ToolParam(type="integer", description="Max depth (default 2)."),
                },
                required=["path"],
            ),
        ),
        ToolInfo(
            name="read_file",
            description="Read a file from the codebase snapshot with line numbers.",
            parameters=ToolParams(
                properties={
                    "path": ToolParam(type="string", description=file_path),
                    "start_line": ToolParam(type="integer", description="First line (default 1)."),
                    "end_line": ToolParam(type="integer", description="Last line (default: end)."),
                },
                required=["path"],
            ),
        ),
        ToolInfo(
            name="grep",
            description="Regex search over the codebase snapshot. Returns path:line: text.",
            parameters=ToolParams(
                properties={
                    "pattern": ToolParam(type="string", description="Python regex."),
                    "path": ToolParam(type="string", description=grep_path),
                },
                required=["pattern"],
            ),
        ),
    ]


# Snapshot paths only: what a snapshot without a container map understands (the judge's only with
# SIMRT_LEGACY_TRANSCRIPTS=1; otherwise it shares the simulator's map).
CODEBASE_TOOLS: list[ToolInfo] = _tools(
    "Directory relative to snapshot root.", "File relative to snapshot root.", "Directory or file to search (default root)."
)
# For a snapshot that also translates container paths (the simulator's and, by default, the judge's).
CONTAINER_CODEBASE_TOOLS: list[ToolInfo] = _tools(
    "Directory relative to snapshot root, or a container path copied from it.",
    "File relative to snapshot root, or a container path copied from it.",
    "Directory or file to search, relative to snapshot root or a container path copied from it (default: "
    "snapshot root).",
)


def codebase_tools(snapshot: CodebaseSnapshot | None) -> list[ToolInfo]:
    """The lookup tools `generate_with_lookups` declares for `snapshot` (callers re-asking without
    tools should declare the same list to keep the prompt cache)."""
    if snapshot is None:
        return []
    return CONTAINER_CODEBASE_TOOLS if snapshot.container_paths else CODEBASE_TOOLS


@dataclass
class ToolLoopResult:
    text: str
    lookups: list[dict[str, object]] = field(default_factory=list)
    usage: list[ModelUsage] = field(default_factory=list)
    stop_reason: str = "stop"
    error: str | None = None
    tool_calls_pending: bool = False  # the final message still asked for a lookup instead of answering


def run_codebase_tool(snapshot: CodebaseSnapshot, call: ToolCall) -> str:
    a = call.arguments
    try:
        if call.function == "list_dir":
            return snapshot.list_dir(str(a.get("path", ".")), min(int(a.get("depth", 2)), 3))
        if call.function == "read_file":
            end = a.get("end_line")
            return snapshot.read_file(str(a["path"]), int(a.get("start_line", 1)), int(end) if end else None)
        if call.function == "grep":
            return snapshot.grep(str(a["pattern"]), str(a.get("path", ".")))
    except Exception as e:  # noqa: BLE001 - a lookup must never abort the simulated step
        return f"error: {type(e).__name__}: {e}"
    return f"error: unknown tool {call.function}"


async def _generate(
    model: Model, convo: list[ChatMessage], tools: list[ToolInfo], tool_choice: ToolChoice, config: GenerateConfig
) -> ModelOutput:
    with message_limit(None):  # simulator/judge chats are not the agent's conversation
        try:
            return await model.generate(input=convo, tools=tools, tool_choice=tool_choice, config=config)
        except Exception as e:
            if tool_choice != "none" or "tool_choice" not in str(e):
                raise
            logger.warning("provider rejected tool_choice=none (%s); retrying with auto", str(e)[:120])
            return await model.generate(input=convo, tools=tools, tool_choice="auto", config=config)


async def generate_with_lookups(
    model: Model,
    messages: list[ChatMessage],
    snapshot: CodebaseSnapshot | None,
    max_lookups: int,
    config: GenerateConfig,
) -> ToolLoopResult:
    """Generate a final text answer, allowing up to `max_lookups` rounds of codebase tool calls.

    `messages` is not mutated: lookup turns are scratch work and are dropped afterwards. The last
    round forbids tool calls (tool_choice="none"); if a model still returns only a tool call, the
    result has `tool_calls_pending=True` and callers must treat it as no answer.
    """
    convo = list(messages)
    result = ToolLoopResult(text="")
    tools = codebase_tools(snapshot)
    for round_idx in range(max_lookups + 1):
        last = round_idx == max_lookups
        if last and round_idx > 0:
            # Keep tools declared (Bedrock rejects tool history without toolConfig) but stop using them.
            convo.append(ChatMessageUser(content=FINAL_ANSWER_NOTE))
        round_tools = [] if last and round_idx == 0 else tools
        choice: ToolChoice = "none" if last and round_tools else "auto"
        out = await _generate(model, convo, round_tools, choice, config)
        if out.usage:
            result.usage.append(out.usage)
        msg = out.message
        if last or not msg.tool_calls:
            result.text = msg.text
            result.stop_reason = str(out.stop_reason)
            result.error = out.error
            result.tool_calls_pending = bool(msg.tool_calls)
            return result
        convo.append(msg)
        for tc in msg.tool_calls:
            assert snapshot is not None
            content = run_codebase_tool(snapshot, tc)
            result.lookups.append({"tool": tc.function, "args": tc.arguments, "chars": len(content)})
            convo.append(ChatMessageTool(content=content, tool_call_id=tc.id, function=tc.function))
    return result


def assistant(text: str) -> ChatMessageAssistant:
    return ChatMessageAssistant(content=text)


def dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=None)
