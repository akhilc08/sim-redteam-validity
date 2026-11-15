"""Render agent transcripts (Inspect chat messages) into compact text for LLM consumption."""

import json
from collections.abc import Sequence

from inspect_ai.model import ChatMessage, ChatMessageAssistant, ChatMessageTool

DEFAULT_OUTPUT_CHARS = 3_000


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars omitted] ...\n{text[-half:]}"


def render_actions(
    messages: Sequence[ChatMessage],
    output_chars: int = DEFAULT_OUTPUT_CHARS,
    total_chars: int | None = None,
) -> str:
    """Tool calls and their outputs only: no system prompt, no agent reasoning or prose.

    This is what an action-only monitor or the success judge sees.
    """
    results: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ChatMessageTool) and m.tool_call_id:
            body = m.error.message if m.error else m.text
            results[m.tool_call_id] = body
    parts: list[str] = []
    step = 0
    for m in messages:
        if not isinstance(m, ChatMessageAssistant) or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            step += 1
            args = json.dumps(tc.arguments, ensure_ascii=False)
            out = _clip(results.get(tc.id, "(no output recorded)"), output_chars)
            parts.append(f"### Step {step}: {tc.function}\nARGS: {args}\nOUTPUT:\n{out}")
    text = "\n\n".join(parts) or "(no tool calls)"
    if total_chars and len(text) > total_chars:
        text = _clip(text, total_chars)
    return text
