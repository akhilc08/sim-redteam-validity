"""Minimal tool loop: let a model consult the codebase snapshot before giving a final answer."""

import json
from dataclasses import dataclass, field

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelUsage,
)
from inspect_ai.tool import ToolCall, ToolInfo, ToolParam, ToolParams

from simrt.codebase import CodebaseSnapshot, PathOutsideSnapshot

CODEBASE_TOOLS: list[ToolInfo] = [
    ToolInfo(
        name="list_dir",
        description="List files under a directory of the codebase snapshot (relative path, depth<=3).",
        parameters=ToolParams(
            properties={
                "path": ToolParam(type="string", description="Directory relative to snapshot root."),
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
                "path": ToolParam(type="string", description="File relative to snapshot root."),
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
                "path": ToolParam(type="string", description="Directory or file to search (default root)."),
            },
            required=["pattern"],
        ),
    ),
]


@dataclass
class ToolLoopResult:
    text: str
    lookups: list[dict[str, object]] = field(default_factory=list)
    usage: list[ModelUsage] = field(default_factory=list)


def run_codebase_tool(snapshot: CodebaseSnapshot, call: ToolCall) -> str:
    a = call.arguments
    try:
        if call.function == "list_dir":
            return snapshot.list_dir(str(a.get("path", ".")), min(int(a.get("depth", 2)), 3))
        if call.function == "read_file":
            end = a.get("end_line")
            return snapshot.read_file(
                str(a["path"]), int(a.get("start_line", 1)), int(end) if end else None
            )
        if call.function == "grep":
            return snapshot.grep(str(a["pattern"]), str(a.get("path", ".")))
    except (PathOutsideSnapshot, KeyError, ValueError, TypeError) as e:
        return f"error: {e}"
    return f"error: unknown tool {call.function}"


async def generate_with_lookups(
    model: Model,
    messages: list[ChatMessage],
    snapshot: CodebaseSnapshot | None,
    max_lookups: int,
    config: GenerateConfig,
) -> ToolLoopResult:
    """Generate a final text answer, allowing up to `max_lookups` rounds of codebase tool calls.

    `messages` is not mutated: lookup turns are scratch work and are dropped afterwards.
    """
    convo = list(messages)
    result = ToolLoopResult(text="")
    tools = CODEBASE_TOOLS if snapshot is not None else []
    for round_idx in range(max_lookups + 1):
        last = round_idx == max_lookups
        if last and round_idx > 0:
            # Keep tools declared (Bedrock rejects tool history without toolConfig) but stop using them.
            convo.append(
                ChatMessageUser(content="Lookup budget exhausted. Give your final answer now, without tool calls.")
            )
        out = await model.generate(input=convo, tools=tools, config=config)
        if out.usage:
            result.usage.append(out.usage)
        msg = out.message
        if last or not msg.tool_calls:
            result.text = msg.text
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
