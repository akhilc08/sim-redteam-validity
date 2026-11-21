"""Render agent transcripts (Inspect chat messages) into compact text for LLM consumption.

Every tool call becomes one step block. Long arguments and outputs are clipped one by one (head and
tail kept, middle replaced by a marker). Over a total budget, outputs shrink first and then
arguments; steps are never dropped, so a long transcript keeps its middle.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass

from inspect_ai.model import ChatMessage, ChatMessageAssistant, ChatMessageTool

DEFAULT_OUTPUT_CHARS = 3_000
MIN_CLIP_CHARS = 200  # floor when shrinking to fit a budget; the budget may overflow at the floor
LEGACY_ENV = "SIMRT_LEGACY_TRANSCRIPTS"


def legacy_transcripts() -> bool:
    """SIMRT_LEGACY_TRANSCRIPTS=1 restores the pre-fix reference and judge inputs (to reproduce old runs)."""
    return os.environ.get(LEGACY_ENV) == "1"


@dataclass(frozen=True)
class Step:
    function: str
    arguments: dict[str, object]
    output: str


def clip(text: str, limit: int) -> str:
    """Keep the head and tail of `text` (limit // 2 chars each) around an omission marker."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars omitted] ...\n{text[-half:]}"


_clip = clip  # old private name, kept for existing callers


def extract_steps(messages: Sequence[ChatMessage]) -> list[Step]:
    """Tool calls with their outputs, in order: no system prompt, no agent reasoning or prose."""
    results: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ChatMessageTool) and m.tool_call_id:
            results[m.tool_call_id] = m.error.message if m.error else m.text
    return [
        Step(tc.function, dict(tc.arguments), results.get(tc.id, "(no output recorded)"))
        for m in messages
        if isinstance(m, ChatMessageAssistant) and m.tool_calls
        for tc in m.tool_calls
    ]


def _arg_text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _clip_args(args: dict[str, object], limit: int | None) -> dict[str, object]:
    """Clip each argument on its own; short values (and every value when `limit` is None) stay as they are."""
    if limit is None:
        return args
    return {k: clip(_arg_text(v), limit) if len(_arg_text(v)) > limit else v for k, v in args.items()}


def _render(steps: Sequence[Step], output_chars: int, arg_chars: int | None) -> str:
    parts = [
        f"### Step {i}: {s.function}\nARGS: {json.dumps(_clip_args(s.arguments, arg_chars), ensure_ascii=False)}\n"
        f"OUTPUT:\n{clip(s.output, output_chars)}"
        for i, s in enumerate(steps, 1)
    ]
    return "\n\n".join(parts) or "(no tool calls)"


def render_steps(
    steps: Sequence[Step],
    output_chars: int = DEFAULT_OUTPUT_CHARS,
    arg_chars: int | None = None,
    total_chars: int | None = None,
) -> str:
    """Clip each output to `output_chars` and each argument to `arg_chars` (None: no limit).

    If the text is still longer than `total_chars`, the output limit shrinks step by step down to
    MIN_CLIP_CHARS, then the argument limit does. Every step is kept, even if that overflows the budget.
    """
    # Start from the longest real value: a larger limit renders the same text.
    longest_out = max((len(s.output) for s in steps), default=0)
    longest_arg = max((len(_arg_text(v)) for s in steps for v in s.arguments.values()), default=0)
    out_lim = min(output_chars, longest_out)
    arg_lim = longest_arg if arg_chars is None else min(arg_chars, longest_arg)
    text = _render(steps, out_lim, arg_lim)
    while total_chars and len(text) > total_chars:
        if out_lim > MIN_CLIP_CHARS:
            out_lim = max(MIN_CLIP_CHARS, out_lim * 3 // 4)
        elif arg_lim > MIN_CLIP_CHARS:
            arg_lim = max(MIN_CLIP_CHARS, arg_lim * 3 // 4)
        else:
            break
        text = _render(steps, out_lim, arg_lim)
    return text


def render_actions(
    messages: Sequence[ChatMessage],
    output_chars: int = DEFAULT_OUTPUT_CHARS,
    total_chars: int | None = None,
    arg_chars: int | None = None,
) -> str:
    """Tool calls and their outputs only: what an action-only monitor or the success judge sees."""
    return render_steps(extract_steps(messages), output_chars, arg_chars, total_chars)


def render_actions_legacy(
    messages: Sequence[ChatMessage], output_chars: int = DEFAULT_OUTPUT_CHARS, total_chars: int | None = None
) -> str:
    """Pre-fix rendering: arguments never clipped; over `total_chars`, the middle of the whole text is cut."""
    text = _render(extract_steps(messages), output_chars, None)
    return clip(text, total_chars) if total_chars and len(text) > total_chars else text
