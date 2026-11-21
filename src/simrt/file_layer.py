"""Deterministic file layer: exact file contents the harness knows, so the simulator model need not guess.

A container file's content is known exactly when
- it was copied from the snapshot into the image (simrt.container_map), is stable there, and
  nothing in this trajectory may have touched it; or
- this trajectory set it: a raw sandbox write_file, an editor `create` that succeeded, or an
  editor edit applied here deterministically.

Any bash command that may write a file (simrt.shell_effects) makes it unknown again; commands that
may write broadly make everything unknown. Unknown files go to the simulator model as before.
Task setup scripts are applied to the container map itself (ContainerImage.apply_setup_script),
and their effects passed to `invalidate`.

`serve_read` answers a plain bash read of known files (cat, head, tail, sed -n, echo; see there)
exactly; any other bash command that names a known file goes to the model with the file attached.
"""

import glob
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from inspect_ai.util import SandboxEnvironmentLimits

from simrt import text_editor_ops
from simrt.container_map import ContainerImage
from simrt.shell_effects import Effects, command_effects, mentioned_paths
from simrt.shell_parse import resolve
from simrt.sim_replies import clip_middle
from simrt.text_editor_ops import EditOutcome, normalized_path

_WILDCARD = re.compile(r"[*?\[]")
# ---- plain reads (serve_read) ----
_WORD_END = frozenset(" \t\n;|&()<>")
_EXPANDS = frozenset("$`*?[]{}~")  # unquoted: an expansion, glob or brace/tilde form -> not served
_STDERR_REDIRECT = re.compile(r"2>(?:/dev/null|&1)(?=[ \t\n;|&]|$)")  # stderr only: a served read writes none
_COUNT = re.compile(r"(\+?)(\d+)")
_SED_PRINT = re.compile(r"(\d+)(?:,(\d+|\$))?p")
_ECHO_OPTION = re.compile(r"-[neE]+")

# Writing any of these makes later commands run code we never see (cron jobs, login-shell start-up
# files sourced by every `bash --login -c` call), so such a write invalidates everything.
HOOK_PATHS = (
    "/etc/cron",
    "/var/spool/cron",
    "/etc/profile",
    "/etc/bash.bashrc",
    "/etc/environment",
    "/etc/ld.so.preload",
    "/root/.bashrc",
    "/root/.profile",
    "/root/.bash_profile",
    "/root/.bash_login",
)
HOOK_NAMES = {".bashrc", ".profile", ".bash_profile", ".bash_login", "crontab", "bash.bashrc", "ld.so.preload"}


def _writes_hook(eff: Effects) -> bool:
    if eff.basenames & HOOK_NAMES:
        return True
    return any(p.startswith(HOOK_PATHS) or any(h.startswith(p.rstrip("/") + "/") for h in HOOK_PATHS) for p in eff.paths)


def _expand_braces(word: str) -> list[str]:
    m = re.search(r"\{([^{}]*,[^{}]*)\}", word)
    if not m:
        return [word]
    return [w for alt in m.group(1).split(",") for w in _expand_braces(word[: m.start()] + alt + word[m.end() :])]


def shell_glob(pattern: str, paths: set[str]) -> set[str]:
    """The paths bash pathname expansion of `pattern` yields (no globstar): braces are expanded, and
    wildcards stay inside one path segment and do not match a leading dot."""
    out: set[str] = set()
    for p in _expand_braces(pattern):
        if not _WILDCARD.search(p):
            out |= {p} & paths
            continue
        rx = re.compile(glob.translate(p, recursive=False, include_hidden=False))
        out |= {c for c in paths if rx.match(c)}
    return out


def _word(command: str, i: int) -> tuple[str, int] | None:
    """The shell word starting at i with its quotes removed, and the index after it; None if bash would
    expand anything in it (variables, substitutions, globs, braces, a tilde) or it is a comment."""
    parts, n, start = [], len(command), i
    while i < n and command[i] not in _WORD_END:
        c = command[i]
        if c == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return None
            parts.append(command[i + 1 : end])
            i = end + 1
        elif c == '"':
            i += 1
            while i < n and command[i] != '"':
                if command[i] in "$`":
                    return None
                if command[i] == "\\" and i + 1 < n and command[i + 1] in '$`"\\\n':
                    parts.append(command[i + 1] if command[i + 1] != "\n" else "")
                    i += 2
                    continue
                parts.append(command[i])
                i += 1
            if i >= n:
                return None
            i += 1
        elif c == "\\":
            if i + 1 >= n:
                return None
            parts.append(command[i + 1] if command[i + 1] != "\n" else "")
            i += 2
        elif c in _EXPANDS or (c == "#" and i == start):
            return None
        else:
            parts.append(c)
            i += 1
    return "".join(parts), i


def _pipelines(command: str) -> list[tuple[str, list[list[str]]]] | None:
    """[(operator joining it to the previous pipeline, its commands' words), ...] for a command line made
    of words, `|`, `&&`, `||`, `;`/newline and stderr redirects (2>/dev/null, 2>&1); None for anything
    else (other redirects, subshells, background jobs, expansions, a dangling operator)."""
    out: list[tuple[str, list[list[str]]]] = []
    joined, pipe, cmd = ";", [], []  # type: ignore[var-annotated]
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c in " \t" or command.startswith("\\\n", i):  # blanks and line continuations
            i += 1 if c in " \t" else 2
            continue
        redirect = _STDERR_REDIRECT.match(command, i)
        op = command[i : i + 2] if command.startswith(("&&", "||"), i) else (";" if c in ";\n" else c)
        if redirect or op in ("&&", "||", ";", "|"):
            if not cmd:  # an operator or a redirect with no command before it
                return None
            i = redirect.end() if redirect else i + (2 if op in ("&&", "||") else 1)
            if not redirect:
                pipe.append(cmd)
                cmd = []
                if op != "|":
                    out.append((joined, pipe))
                    joined, pipe = op, []
            continue
        word = None if c in "&()<>" else _word(command, i)
        if word is None:
            return None
        cmd.append(word[0])
        i = word[1]
    if cmd:
        out.append((joined, [*pipe, cmd]))
    elif pipe or joined != ";":  # ends in `|`, `&&` or `||`
        return None
    return out


def _lines(text: str) -> list[str]:
    """Lines as head/tail/sed count them: split at "\\n" only, a last line without one included."""
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _line_option(args: list[str], plus_ok: bool) -> tuple[int, bool, list[str]] | None:
    """head/tail arguments: (line count, counted from the start (`+N`), file operands); default 10 lines.
    One count option at most (-n N, -nN, -N, --lines=N, and for tail +N); anything else is not served."""
    count, files, i = None, [], 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--lines") and i + 1 < len(args) and count is None:
            count, i = args[i + 1], i + 2
        elif (a.startswith("-n") and len(a) > 2 or a.startswith("--lines=")) and count is None:
            count, i = a[2:] if a.startswith("-n") else a[8:], i + 1
        elif re.fullmatch(r"-\d+", a) and count is None:
            count, i = a[1:], i + 1
        elif a.startswith("-"):
            return None
        else:
            files.append(a)
            i += 1
    m = _COUNT.fullmatch("10" if count is None else count)
    if not m or (m.group(1) and (not plus_ok or m.group(2).lstrip("0") == "")):
        return None
    return int(m.group(2)), bool(m.group(1)), files


@dataclass(frozen=True)
class KnownFile:
    path: str
    text: str
    source: str  # "snapshot <rel>" or "this trajectory"


@dataclass
class FileLayer:
    image: ContainerImage
    enabled: bool = True
    known: dict[str, str] = field(default_factory=dict)  # exact current contents set in this trajectory
    touched: set[str] = field(default_factory=set)  # snapshot files that may have changed
    all_touched: bool = False

    # ---- reads -------------------------------------------------------------------------------
    def raw_bytes(self, path: str) -> bytes | None:
        """Exact current bytes of a file, or None if not known."""
        if not self.enabled:
            return None
        p = normalized_path(path)
        if p is None:
            return None
        if p in self.known:
            return self.known[p].encode()
        rel = None if self.all_touched or p in self.touched else self.image.source_of(p)
        if rel is None or self.image.root is None:
            return None
        try:
            return (self.image.root / rel).read_bytes()
        except OSError:
            return None

    def content(self, path: str) -> KnownFile | None:
        """Exact current text of a file (UTF-8), or None if not known."""
        data = self.raw_bytes(path)
        if data is None:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        p = normalized_path(path) or path
        rel = self.image.source_of(p)
        return KnownFile(p, text, "this trajectory" if p in self.known or rel is None else f"snapshot {rel}")

    def mentioned(self, command: str, cwd: str | None, file_cap: int, total_cap: int) -> list[KnownFile]:
        """Known files a bash command names (exact paths and globs), within the size caps."""
        if not self.enabled:
            return []
        paths, globs = mentioned_paths(command, cwd)
        candidates = set(self.known)
        if not self.all_touched:
            candidates |= {p for p in self.image.files if p not in self.touched}
        for pattern in globs:
            paths |= shell_glob(pattern, candidates)
        out: list[KnownFile] = []
        budget = total_cap
        for p in sorted(paths):
            f = self.content(p)
            if f is None or budget <= 0:
                continue
            cap = min(file_cap, budget)
            if len(f.text) > cap:  # keep both ends: `tail`, and the end of a long output, need them
                omitted = len(f.text) - cap
                f = KnownFile(f.path, clip_middle(f.text, cap), f"{f.source}, {omitted} of {len(f.text)} chars omitted in the middle")
            out.append(f)
            budget -= len(f.text)
        return out

    def serve_read(self, command: str, cwd: str) -> str | None:
        """Exact stdout of a plain read of known files, or None (the simulator model answers instead).

        Plain: `cat FILE...` (no options), `head`/`tail` [-n N | -N | tail's -n +N] [FILE], `sed -n 'Np'`,
        `'N,Mp'` or `'N,$p'` [FILE], and `echo` of literal words; a command without FILE reads the pipe
        before it. Commands are joined by `|`, `&&`, `||` and `;`, with stderr optionally sent to /dev/null
        or stdout, and at least one known file is read. Each such command succeeds on a known file and
        writes no stderr, so what follows `||` never runs and the exit code is 0. Run as root, as
        ct's bash tool runs it."""
        pipelines = _pipelines(command) if self.enabled else None
        if not pipelines:
            return None
        out: list[str] = []
        read: list[str] = []  # paths of the known files read
        for joined, pipe in pipelines:
            if joined == "||":  # the commands before it succeeded
                continue
            text: str | None = None
            for words in pipe:
                if (text := self._read_command(words[0], words[1:], text, cwd, read)) is None:
                    return None
            out.append(text)  # type: ignore[arg-type]
        stdout = "".join(out)
        return stdout if read and len(stdout.encode()) <= SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE else None

    def _read_command(self, prog: str, args: list[str], stdin: str | None, cwd: str, read: list[str]) -> str | None:
        if prog == "echo":
            return None if args and _ECHO_OPTION.fullmatch(args[0]) else " ".join(args) + "\n"
        files: list[str] = args
        if prog == "head" or prog == "tail":
            if (opt := _line_option(args, plus_ok=prog == "tail")) is None:
                return None
            count, from_start, files = opt
        elif prog == "sed":
            m = _SED_PRINT.fullmatch(args[1]) if len(args) in (2, 3) and args[0] == "-n" else None
            if m is None or int(m.group(1)) == 0:
                return None
            files = args[2:]
        elif prog != "cat" or any(a.startswith("-") for a in args):
            return None
        texts = []
        for word in files:  # `x/..` is left to the model: it fails in a container where x does not exist
            known = self.content(path) if ".." not in word.split("/") and (path := resolve(word, cwd)) else None
            if known is None:
                return None
            texts.append(known.text)
            read.append(known.path)
        if prog == "cat":
            return "".join(texts) if texts else stdin
        if len(texts) > 1 or (not texts and stdin is None):  # several files get headers; no input at all
            return None
        lines = _lines(texts[0] if texts else stdin)  # type: ignore[arg-type]
        if prog == "sed":
            first = int(m.group(1))  # type: ignore[union-attr]
            last = len(lines) if m.group(2) == "$" else int(m.group(2) or first)  # type: ignore[union-attr]
            return "".join(lines[first - 1 : max(first, last)])  # N,M with M < N prints line N only
        if prog == "head":
            return "".join(lines[:count])
        return "".join(lines[count - 1 :] if from_start else lines[-count:] if count else [])

    # ---- state changes -----------------------------------------------------------------------
    def invalidate(self, eff: Effects) -> None:
        if not self.enabled:
            return
        if eff.everything or _writes_hook(eff):
            self.known.clear()
            self.all_touched = True
            return
        for p in [p for p in self.known if eff.covers(p)]:
            del self.known[p]
        self.touched |= {p for p in self.image.files if eff.covers(p)}

    def after_bash(self, command: str, cwd: str | None) -> None:
        if self.enabled:
            self.invalidate(command_effects(command, cwd))

    def record_write(self, path: str, contents: str, exact: bool = True) -> None:
        """A raw sandbox write; `exact=False` when the bytes could not be decoded losslessly."""
        if not self.enabled:
            return
        p = normalized_path(path)
        if p is None:
            self.invalidate(Effects(basenames={posixpath.basename(path)}))
            return
        self.invalidate(Effects(paths={p}))
        if exact:
            self.known[p] = contents

    def editor(self, params: dict[str, Any]) -> EditOutcome | None:
        """Apply a text_editor call exactly if the outcome is determined; None means ask the model."""
        if not self.enabled:
            return None
        f = self.content(str(params.get("path")))
        outcome = text_editor_ops.apply(params, f.text if f else None)
        if outcome is not None and outcome.new_content is not None and f is not None:
            self.invalidate(Effects(paths={f.path}))
            self.known[f.path] = outcome.new_content
        return outcome

    def after_model_editor(self, params: dict[str, Any], ok: bool | None) -> None:
        """Update state after the simulator model answered an editor call (ok=None: no valid answer)."""
        command = params.get("command")
        if command == "view" or not self.enabled:
            return
        path = normalized_path(params.get("path"))
        if path is None:  # relative or odd path: the tool resolves it somewhere we cannot tell
            self.invalidate(Effects(basenames={posixpath.basename(str(params.get("path")))}))
            return
        self.invalidate(Effects(paths={path}))
        file_text = params.get("file_text")
        if command == "create" and ok and isinstance(file_text, str):
            self.known[path] = file_text
