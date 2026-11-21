"""Read-only access to an environment's codebase snapshot (SCE-style simulator grounding).

Lookup paths are snapshot-relative. Absolute paths are tried as snapshot paths first and then as
container paths copied from the snapshot (`container_paths`, from simrt.container_map); a path
that is neither gets an explicit error rather than an empty answer. Container paths the image
deletes before the agent starts (`deleted`) are reported as absent, and copied files that change at
run time (`volatile`) carry a warning.
"""

import posixpath
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # the `regex` module supports a per-search timeout; fall back to `re` without one
    import regex as _rx
except ImportError:  # pragma: no cover - present in the experiment venv
    _rx = None

MAX_READ_CHARS = 40_000
MAX_LIST_ENTRIES = 400
MAX_GREP_HITS = 80
MAX_GREP_LINE = 300
MAX_GREP_FILE_BYTES = 2_000_000
GREP_SECONDS = 5.0
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "target", "vendor"}


class PathOutsideSnapshot(ValueError):
    """Raised when a requested path escapes the snapshot root or is not in the snapshot."""


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


@dataclass(frozen=True)
class CodebaseSnapshot:
    """A directory tree the simulator/judge may inspect."""

    root: Path
    container_paths: tuple[tuple[str, str], ...] = ()  # (container path, snapshot path), files and dirs
    deleted: tuple[tuple[str, str], ...] = ()  # (container path, snapshot path) removed before the agent starts
    volatile: frozenset[str] = frozenset()  # copied container files whose content changes at run time

    def _resolve(self, rel: str) -> Path:
        rel = (rel or ".").lstrip("/")
        root = self.root.resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise PathOutsideSnapshot(f"path escapes snapshot: {rel}")
        return target

    def _locate(self, path: str) -> tuple[Path, str]:
        """(snapshot path, note) for a lookup path; raises PathOutsideSnapshot if it is not in the snapshot."""
        direct = self._resolve(path)
        if not path.startswith("/") or direct.exists() or not path.strip("/"):
            return direct, ""
        clean = posixpath.normpath("/" + path.strip("/"))
        gone = next(((c, s) for c, s in self.deleted if _under(clean, c)), None)
        if gone is not None:
            raise PathOutsideSnapshot(
                f"{clean} does not exist in the container: {gone[0]} (copied from snapshot path {gone[1]}) is "
                "deleted before the agent starts, at container start-up or by a setup script"
            )
        for container, snap in sorted(self.container_paths, key=lambda p: -len(p[0])):
            if _under(clean, container):
                suffix = clean[len(container.rstrip("/")) :].lstrip("/")
                rel = posixpath.normpath(posixpath.join(snap, suffix))
                target = self._resolve(rel)
                if target.exists():
                    return target, self._note(clean, rel, target)
        if not self.container_paths:
            raise PathOutsideSnapshot(
                f"{path} is not in the snapshot (snapshot root = {self.root.parent.name}/{self.root.name}; "
                "lookup paths are relative to it, and container paths are not translated here)"
            )
        dirs = ", ".join(f"{c} <- {s}" for c, s in self.container_paths if (self.root / s).is_dir())
        raise PathOutsideSnapshot(
            f"{path} is not in the snapshot: it is neither a snapshot path nor a container path copied from "
            f"it (snapshot root = {self.root.parent.name}/{self.root.name}; copied directories: {dirs or 'none'})"
        )

    def _note(self, container: str, rel: str, target: Path) -> str:
        note = f"[container path {container} = snapshot path {rel}"
        if container in self.volatile:
            note += "; changed at start-up, by a setup script or by scheduled jobs, so the container's content may differ"
        gone = [c for c, _ in self.deleted if target.is_dir() and _under(c, container)]
        if gone:
            note += f"; deleted before the agent starts, so not in the container: {', '.join(gone)}"
        return note + "]\n"

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.root.resolve()))

    def _inside(self, p: Path) -> bool:
        root = self.root.resolve()
        resolved = p.resolve()
        return resolved == root or root in resolved.parents

    def list_dir(self, rel: str = ".", depth: int = 2) -> str:
        base, note = self._locate(rel)
        if not base.is_dir():
            return f"error: not a directory: {rel}"
        out: list[str] = []
        base_depth = len(base.parts)
        for p in sorted(base.rglob("*")):
            if any(part in SKIP_DIRS for part in p.relative_to(base).parts):
                continue
            if len(p.parts) - base_depth > depth:
                continue
            out.append(self._rel(p) + ("/" if p.is_dir() else ""))
            if len(out) >= MAX_LIST_ENTRIES:
                out.append("... (truncated)")
                break
        return note + ("\n".join(out) or "(empty)")

    def read_file(self, rel: str, start_line: int = 1, end_line: int | None = None) -> str:
        path, note = self._locate(rel)
        if not path.is_file():
            return f"error: no such file in snapshot: {rel}"
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError as e:
            return f"error: {e}"
        start = max(start_line, 1)
        end = None if end_line is None or end_line < 1 else end_line
        chunk = lines[start - 1 : end]
        text = "\n".join(f"{i:6}\t{line}" for i, line in enumerate(chunk, start))
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + "\n... (truncated; request a narrower line range)"
        return f"{note}{rel} ({len(lines)} lines)\n{text}"

    def _grep_files(self, base: Path) -> Iterator[Path]:
        if base.is_file():
            yield base
            return
        for f in sorted(base.rglob("*")):  # rglob does not descend into symlinked directories
            if any(part in SKIP_DIRS for part in f.relative_to(base).parts[:-1]):
                continue
            if f.is_file() and self._inside(f):  # skip symlinks that point outside the snapshot
                yield f

    def grep(self, pattern: str, rel: str = ".") -> str:
        base, note = self._locate(rel)
        if not base.exists():
            return f"error: no such file or directory in snapshot: {rel}"
        try:
            rx: Any = (_rx or re).compile(pattern)
        except Exception as e:  # noqa: BLE001 - re.error / regex.error
            return f"error: bad regex: {e}"
        deadline = time.monotonic() + GREP_SECONDS
        stopped = f"... (stopped after {GREP_SECONDS:.0f}s; results incomplete)"
        hits: list[str] = []
        searched = 0
        for f in self._grep_files(base):
            if time.monotonic() > deadline:
                hits.append(stopped)
                break
            try:
                if f.stat().st_size > MAX_GREP_FILE_BYTES:
                    hits.append(f"... (skipped {self._rel(f)}: larger than {MAX_GREP_FILE_BYTES} bytes)")
                    continue
                text = f.read_text(errors="replace")
            except OSError:
                continue
            searched += 1
            for n, line in enumerate(text.splitlines(), 1):
                left = deadline - time.monotonic()  # the overall bound also holds inside one large file
                if left <= 0:
                    hits.append(stopped)
                    return note + "\n".join(hits)
                try:
                    found = rx.search(line, timeout=min(1.0, left)) if _rx else rx.search(line)
                except TimeoutError:
                    hits.append(f"... (regex timed out on {self._rel(f)}:{n}; results incomplete)")
                    break
                if found:
                    shown = line if len(line) <= MAX_GREP_LINE else f"{line[:MAX_GREP_LINE]}... [+{len(line) - MAX_GREP_LINE} chars]"
                    hits.append(f"{self._rel(f)}:{n}: {shown}")
                    if len(hits) >= MAX_GREP_HITS:
                        return note + "\n".join(hits) + "\n... (truncated)"
        return note + ("\n".join(hits) or f"(no matches in {searched} files under {rel})")

    def read_optional(self, rel: str, limit: int = 20_000) -> str | None:
        """Raw file text (no line numbers) if it exists, for building static context."""
        try:
            path = self._resolve(rel)
        except PathOutsideSnapshot:
            return None
        if not path.is_file():
            return None
        return path.read_text(errors="replace")[:limit]
