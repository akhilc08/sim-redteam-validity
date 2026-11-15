"""Read-only access to an environment's codebase snapshot (SCE-style simulator grounding)."""

import re
from dataclasses import dataclass
from pathlib import Path

MAX_READ_CHARS = 40_000
MAX_LIST_ENTRIES = 400
MAX_GREP_HITS = 80
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "target", "vendor"}


class PathOutsideSnapshot(ValueError):
    """Raised when a requested path escapes the snapshot root."""


@dataclass(frozen=True)
class CodebaseSnapshot:
    """A directory tree the simulator/judge may inspect. All paths are relative to `root`."""

    root: Path

    def _resolve(self, rel: str) -> Path:
        rel = (rel or ".").lstrip("/")
        root = self.root.resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise PathOutsideSnapshot(f"path escapes snapshot: {rel}")
        return target

    def list_dir(self, rel: str = ".", depth: int = 2) -> str:
        base = self._resolve(rel)
        if not base.is_dir():
            return f"error: not a directory: {rel}"
        out: list[str] = []
        base_depth = len(base.parts)
        for p in sorted(base.rglob("*")):
            if any(part in SKIP_DIRS for part in p.relative_to(base).parts):
                continue
            if len(p.parts) - base_depth > depth:
                continue
            out.append(str(p.relative_to(self.root.resolve())) + ("/" if p.is_dir() else ""))
            if len(out) >= MAX_LIST_ENTRIES:
                out.append("... (truncated)")
                break
        return "\n".join(out) or "(empty)"

    def read_file(self, rel: str, start_line: int = 1, end_line: int | None = None) -> str:
        path = self._resolve(rel)
        if not path.is_file():
            return f"error: no such file in snapshot: {rel}"
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError as e:
            return f"error: {e}"
        start = max(start_line, 1)
        chunk = lines[start - 1 : end_line]
        text = "\n".join(f"{i:6}\t{line}" for i, line in enumerate(chunk, start))
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + "\n... (truncated; request a narrower line range)"
        return f"{rel} ({len(lines)} lines)\n{text}"

    def grep(self, pattern: str, rel: str = ".") -> str:
        base = self._resolve(rel)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"error: bad regex: {e}"
        files = [base] if base.is_file() else sorted(base.rglob("*"))
        hits: list[str] = []
        for f in files:
            if not f.is_file() or any(part in SKIP_DIRS for part in f.parts):
                continue
            try:
                for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(self.root.resolve())}:{n}: {line[:300]}")
                        if len(hits) >= MAX_GREP_HITS:
                            return "\n".join(hits) + "\n... (truncated)"
            except OSError:
                continue
        return "\n".join(hits) or "(no matches)"

    def read_optional(self, rel: str, limit: int = 20_000) -> str | None:
        """Raw file text (no line numbers) if it exists, for building static context."""
        try:
            path = self._resolve(rel)
        except PathOutsideSnapshot:
            return None
        if not path.is_file():
            return None
        return path.read_text(errors="replace")[:limit]
