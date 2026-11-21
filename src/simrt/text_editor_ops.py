"""Exact `text_editor` semantics for files whose current content the harness knows.

Mirrors the tool-support server that real runs use (inspect-sandbox-tools v5,
`inspect_sandbox_tools/_in_process_tools/_text_editor/text_editor.py`, read from the bundled
binary): same checks in the same order, same messages, tabs expanded on edit, 16,000-char view
truncation. Only operations whose outcome is fully determined by the known content are handled;
everything else returns None and goes to the simulator model.
"""

import posixpath
from dataclasses import dataclass
from typing import Any

SNIPPET_LINES = 4
MAX_RESPONSE_LEN = 16_000
TRUNCATED_MESSAGE = (
    "<response clipped><NOTE>To save on context only part of this file has been shown to you. You "
    "should retry this tool after you have searched inside the file with `grep -n` in order to find "
    "the line numbers of what you are looking for.</NOTE>"
)
REVIEW = "Review the changes and make sure they are as expected. Edit the file again if necessary."
REVIEW_INSERT = (
    "Review the changes and make sure they are as expected (correct indentation, no duplicate lines, "
    "etc). Edit the file again if necessary."
)


@dataclass(frozen=True)
class EditOutcome:
    ok: bool
    text: str
    new_content: str | None = None  # file content after a successful write


def normalized_path(path: object) -> str | None:
    """The path the tool would report, if it is absolute and already normalised; else None."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    return path if posixpath.normpath(path) == path else None


def _read_text(content: str) -> str:
    """`Path.read_text()` uses universal newlines."""
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _make_output(content: str, descriptor: str, init_line: int = 1) -> str:
    if len(content) > MAX_RESPONSE_LEN:
        content = content[:MAX_RESPONSE_LEN] + TRUNCATED_MESSAGE
    numbered = "\n".join(f"{i + init_line:6}\t{line}" for i, line in enumerate(content.expandtabs().split("\n")))
    return f"Here's the result of running `cat -n` on {descriptor}:\n{numbered}\n"


def _error(text: str) -> EditOutcome:
    return EditOutcome(False, text)


def view(path: str, content: str, view_range: Any) -> EditOutcome:
    init_line = 1
    if view_range:
        if not isinstance(view_range, list) or len(view_range) != 2 or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in view_range
        ):
            return _error("Invalid `view_range`. It should be a list of two integers.")
        lines = content.split("\n")
        n = len(lines)
        init_line, final_line = view_range
        if init_line < 1 or init_line > n:
            return _error(
                f"Invalid `view_range`: {view_range}. Its first element `{init_line}` should be within "
                f"the range of lines of the file: {[1, n]}"
            )
        if final_line > n:
            final_line = -1
        if final_line != -1 and final_line < init_line:
            return _error(
                f"Invalid `view_range`: {view_range}. Its second element `{final_line}` should be larger "
                f"or equal than its first `{init_line}`"
            )
        content = "\n".join(lines[init_line - 1 :] if final_line == -1 else lines[init_line - 1 : final_line])
    return EditOutcome(True, _make_output(content, path, init_line))


def str_replace(path: str, content: str, old_str: str, new_str: str | None) -> EditOutcome:
    file_content = content.expandtabs()
    old_str = old_str.expandtabs()
    new_str = new_str.expandtabs() if new_str is not None else ""
    occurrences = file_content.count(old_str)
    if occurrences == 0:
        return _error(f"No replacement was performed, old_str `{old_str}` did not appear verbatim in {path}.")
    if occurrences > 1:
        lines = [i + 1 for i, line in enumerate(file_content.split("\n")) if old_str in line]
        return _error(
            f"No replacement was performed. Multiple occurrences of old_str `{old_str}` in lines {lines}. "
            "Please ensure it is unique"
        )
    new_content = file_content.replace(old_str, new_str)
    replacement_line = file_content.split(old_str)[0].count("\n")
    start_line = max(0, replacement_line - SNIPPET_LINES)
    end_line = replacement_line + SNIPPET_LINES + new_str.count("\n")
    snippet = "\n".join(new_content.split("\n")[start_line : end_line + 1])
    text = f"The file {path} has been edited. " + _make_output(snippet, f"a snippet of {path}", start_line + 1)
    return EditOutcome(True, text + REVIEW, new_content)


def insert(path: str, content: str, insert_line: int, new_str: str) -> EditOutcome:
    file_text = content.expandtabs()
    new_str = new_str.expandtabs()
    lines = file_text.split("\n")
    n = len(lines)
    if insert_line < 0 or insert_line > n:
        return _error(
            f"Invalid `insert_line` parameter: {insert_line}. It should be within the range of lines of "
            f"the file: {[0, n]}"
        )
    new_lines = new_str.split("\n")
    new_content = "\n".join(lines[:insert_line] + new_lines + lines[insert_line:])
    snippet_lines = (
        lines[max(0, insert_line - SNIPPET_LINES) : insert_line]
        + new_lines
        + lines[insert_line : insert_line + SNIPPET_LINES]
    )
    start = max(1, insert_line - SNIPPET_LINES + 1)
    text = f"The file {path} has been edited. " + _make_output("\n".join(snippet_lines), "a snippet of the edited file", start)
    return EditOutcome(True, text + REVIEW_INSERT, new_content)


def apply(params: dict[str, Any], content: str | None) -> EditOutcome | None:
    """Run a text_editor call on a file whose current raw content is `content` (None = unknown)."""
    command = params.get("command")
    if command == "str_replace" and params.get("old_str") == "":  # checked before the path
        return _error(
            "str_replace: The `old_str` parameter cannot be empty. Consider using the `insert` command instead."
        )
    path = normalized_path(params.get("path"))
    if content is None or path is None:
        return None
    text = _read_text(content)
    if command == "view":
        return view(path, text, params.get("view_range"))
    if command == "create":
        return _error(f"File already exists at: {path}. Cannot overwrite files using command `create`.")
    if command == "str_replace" and isinstance(params.get("old_str"), str):
        new_str = params.get("new_str")
        return str_replace(path, text, params["old_str"], new_str if isinstance(new_str, str) else None)
    insert_line = params.get("insert_line")
    if command == "insert" and isinstance(insert_line, int) and isinstance(params.get("new_str"), str):
        return insert(path, text, insert_line, params["new_str"])
    return None  # undo_edit, malformed parameters: left to the simulator model
