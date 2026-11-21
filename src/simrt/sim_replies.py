"""Parse, validate and render simulator replies. Invalid replies are rejected, never guessed at.

A bash reply is valid when its first block has an integer <exit_code>, every <stdout>/<stderr> it
opens is closed, and any output is inside those tags (or it is a lone <timeout/> for a call that
has a timeout). An editor reply is valid when its first <result> or <error> block is closed and
non-empty. Text after the first block (a runaway continuation of the session) is ignored.
"""

import re
from dataclasses import dataclass

SIM_ERROR_PREFIX = "simulator error:"
_NEXT_CALL = re.compile(r"\n\[(?:bash|text_editor|read_file)\]")
_REPLY_TAG = re.compile(r"</?(?:exit_code|stdout|stderr|result|error)>|<timeout\s*/?>")


@dataclass(frozen=True)
class BashResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class EditorResult:
    ok: bool
    text: str


def _first_block_end(text: str, opener: str) -> int:
    """End of the first reply block: before a second `opener` or a hallucinated next call."""
    ends = [len(text)]
    first = text.find(opener)
    if first >= 0 and (second := text.find(opener, first + len(opener))) >= 0:
        ends.append(second)
    if (m := _NEXT_CALL.search(text)) is not None:
        ends.append(m.start())
    return min(ends)


def _section(region: str, name: str, followers: tuple[str, ...]) -> tuple[str | None, str | None]:
    """(content, problem) of <name>...</name> in `region`; content None if the tag is absent.

    The closing tag preferred is one followed only by whitespace and another section (or the end),
    so output that itself contains the closing tag survives.
    """
    start = region.find(f"<{name}>")
    if start < 0:
        return None, None
    body_start = start + len(name) + 2
    after = "|".join(re.escape(f) for f in followers)
    m = re.compile(rf"</{name}>(?=\s*(?:{after}|$))" if after else rf"</{name}>(?=\s*$)").search(region, body_start)
    end = m.start() if m else region.find(f"</{name}>", body_start)
    if end < 0:
        return None, f"unclosed <{name}> (reply cut off)"
    body = region[body_start:end]
    body = body[1:] if body.startswith("\n") else body
    return (body[:-1] if body.endswith("\n") else body), None


def check_bash(text: str, timeout_allowed: bool = True) -> tuple[BashResult | None, str | None]:
    """(result, None) for a valid bash reply, else (None, reason)."""
    timeout = re.search(r"<timeout\s*/?>", text)
    code_at = text.find("<exit_code>")
    if timeout and (code_at < 0 or timeout.start() < code_at):
        if not timeout_allowed:
            return None, "reported a timeout for a call without a timeout"
        return BashResult(124, "", "", timed_out=True), None
    if code_at < 0:
        return None, "empty reply" if not text.strip() else "missing <exit_code> tag"
    result, problem = _bash_block(text[: _first_block_end(text, "<exit_code>")])
    if problem and "cut off" in problem:  # the output may legitimately contain a line like "[bash] ..."
        full, _ = _bash_block(text)
        if full is not None and not _REPLY_TAG.search(full.stdout + full.stderr):  # not a made-up next call
            return full, None
    return result, problem


def _bash_block(region: str) -> tuple[BashResult | None, str | None]:
    code_s, problem = _section(region, "exit_code", ("<stdout>", "<stderr>"))
    if problem or code_s is None:
        return None, problem or "missing <exit_code> tag"
    m = re.match(r"\s*(-?\d+)\s*(\(.*\))?\s*$", code_s, re.DOTALL)
    if not m:
        return None, f"exit code is not an integer: {code_s.strip()[:40]!r}"
    stdout, problem = _section(region, "stdout", ("<stderr>", "<exit_code>"))
    if problem:
        return None, problem
    stderr, problem = _section(region, "stderr", ("<stdout>", "<exit_code>"))
    if problem:
        return None, problem
    if stdout is None:  # without <stdout>, any other text would be output silently dropped
        outside = re.sub(
            r"<exit_code>.*?</exit_code>|<stderr>.*</stderr>|<(?:stdout|stderr)\s*/>", "", region, flags=re.DOTALL
        )
        if outside.strip():
            return None, "output outside <stdout> tags"
    return BashResult(int(m.group(1)), stdout or "", stderr or ""), None


def check_editor(text: str) -> tuple[EditorResult | None, str | None]:
    """(result, None) for a valid editor reply, else (None, reason)."""
    starts = {tag: text.find(f"<{tag}>") for tag in ("result", "error")}
    present = {t: i for t, i in starts.items() if i >= 0}
    if not present:
        return None, "empty reply" if not text.strip() else "missing <result>/<error> tag"
    tag = min(present, key=lambda t: present[t])
    other = "error" if tag == "result" else "result"
    region = text[: _first_block_end(text[present[tag] :], f"<{tag}>") + present[tag]]
    body, problem = _section(region, tag, (f"<{other}>",))
    if problem and "cut off" in problem:  # the output may legitimately contain a line like "[bash] ..."
        full, _ = _section(text, tag, (f"<{other}>",))
        if full is not None and not _REPLY_TAG.search(full):  # not a made-up next call
            body, problem = full, None
    if problem or body is None:
        return None, problem or f"missing <{tag}> tag"
    if not body.strip():
        return None, f"empty <{tag}>"
    return EditorResult(tag == "result", body), None


def bash_failure(reason: str) -> BashResult:
    return BashResult(1, "", f"{SIM_ERROR_PREFIX} {reason}")


def editor_failure(reason: str) -> EditorResult:
    return EditorResult(False, f"{SIM_ERROR_PREFIX} {reason}")


def parse_bash(text: str) -> BashResult:
    """Fail-closed parse: an invalid reply becomes a visible simulator error, never empty success."""
    result, reason = check_bash(text)
    return result if result is not None else bash_failure(reason or "invalid reply")


def parse_editor(text: str) -> EditorResult:
    result, reason = check_editor(text)
    return result if result is not None else editor_failure(reason or "invalid reply")


def clip_middle(text: str, limit: int) -> str:
    """Keep the head and tail of `text` with an explicit marker for what was dropped."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} chars omitted ...]\n{text[-tail:]}"


def render_bash(r: BashResult, limit: int = 0) -> str:
    """Canonical form stored in history, so stray model text never leaks into later context."""
    if r.timed_out:
        return "<timeout/>"
    stdout = clip_middle(r.stdout, limit)
    stderr = clip_middle(r.stderr, limit // 4 if limit else 0)
    return f"<exit_code>{r.exit_code}</exit_code>\n<stdout>\n{stdout}\n</stdout>\n<stderr>\n{stderr}\n</stderr>"


def render_editor(r: EditorResult, limit: int = 0) -> str:
    tag = "result" if r.ok else "error"
    return f"<{tag}>\n{clip_middle(r.text, limit)}\n</{tag}>"
