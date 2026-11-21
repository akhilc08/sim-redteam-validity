"""Conservative static analysis of shell command lines: which files could a command write or delete?

Used by the deterministic file layer (simrt.file_layer) for agent commands, and by the container
path map (simrt.container_map) for Dockerfile RUN steps, start-up scripts, cron jobs and task setup
scripts. Anything that cannot be classified counts as writing everything. Which programs write with
which arguments is decided by simrt.shell_programs.

Three modes:
- strict (agent commands, `read_script=None`): running a script, an interpreter, a build tool or
  any unknown program may write anywhere -> `everything`.
- static (`read_script` given): scripts that are part of the image are read and analysed
  recursively; other programs (services) count as writing only the paths they mention. Build
  tools (package managers, git, archivers, ...) still count as writing everything.
- static with `unknown_code=True` (task setup scripts, which really run before the agent starts):
  as static, but code the analysis cannot see may write anything: a script that is not in the
  image, a program read from a pipe or file (`curl ... | bash`, `bash -s < f`), `python -m`.

Deletions (rm/unlink/rmdir operands, the source of `mv`) are tracked in order: a later write to a
path re-creates it.
"""

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from simrt.shell_parse import UNRESOLVED, SimpleCommand, expand, option_value, prepare, resolve, simple_commands
from simrt.shell_programs import (
    EVERYTHING,
    MENTIONED,
    READ,
    READ_ONLY,
    classify,
    copied_pair,
    deleted_words,
    written_words,
)

__all__ = ["READ_ONLY", "Effects", "command_effects", "mentioned_paths", "scan_code"]

METADATA_ONLY = frozenset("chmod chown chgrp mkdir touch umask ulimit".split())
# Programs that write (or delete) only the paths they are given.
TARGETED = frozenset(
    "cp mv rm rmdir ln install truncate tee dd shred unlink split csplit vi vim nano ed ex emacs sponge".split()
)
# Programs that may rewrite many files without naming them: `everything` in both modes. (Service
# managers are not here: in strict mode every unknown program already means `everything`, while a
# start-up script starting a service should not mark the whole image as changed.)
BUILD_TOOLS = frozenset(
    "npm npx yarn pnpm pip pip3 uv poetry pipenv conda cargo rustup go make cmake ninja mvn gradle bundle "
    "bundler gem composer git patch tar unzip zip gunzip gzip bzip2 xz 7z rsync scp apt apt-get aptitude "
    "dpkg yum dnf apk snap brew alembic prisma rake mix dotnet xargs parallel eval find".split()
)
INTERPRETERS = frozenset(
    "bash sh dash zsh ksh python python2 python3 node nodejs Rscript R ruby perl php deno bun tsx ts-node "
    "lua julia mongo mongosh".split()
)
SHELLS = frozenset("bash sh dash zsh ksh".split())
_VERSION_FLAGS = frozenset(("--version", "-V", "-v", "--help", "-h"))
_INTERPRETER_VALUE_FLAGS = frozenset(("-o", "+o", "-O", "+O", "-W", "-X"))  # bash set options, python -W/-X
_SYSTEM_DIRS = ("/usr/", "/bin/", "/sbin/", "/lib/", "/etc/init.d/")
_DECLARE = frozenset("export local declare readonly typeset".split())
_PREFIXES = frozenset("sudo doas nohup exec builtin setsid unbuffer time nice ionice stdbuf chrt taskset timeout".split())
_PREFIX_ARG_OPTS = frozenset("-u -g -n -c -s -k -C -p -U".split())
_KEYWORDS = frozenset("if then else elif fi do done while until ! { } time in esac coproc".split())
_BINDINGS = frozenset("for case select function".split())
_READ_SUBCOMMANDS = {
    "git": {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "grep", "describe", "shortlog"},
    "pip": {"list", "show", "freeze", "check", "help", "--version", "-V"},
    "npm": {"ls", "list", "view", "info", "help", "outdated", "root", "prefix", "--version", "-v"},
    "systemctl": {"status", "is-active", "is-enabled", "list-units", "show"},
    "crontab": {"-l"},
}
_GLOB = re.compile(r"[*?\[\0]|\{[^}]*,[^}]*\}")
_ASSIGNMENT = re.compile(r"^([A-Za-z_]\w*)(\[[^\]]*\])?\+?=(.*)$", re.DOTALL)
_ABS_PATH = re.compile(r"(?<![\w.~$-])/(?:[\w.@+%-]+/?)+")
_QUOTED_NAME = re.compile(r"""['"]([\w.-]+\.\w{1,8})['"]""")
_WRITE_PRIMITIVE = re.compile(
    r"open\([^)]*['\"][wax+]|write_text|write_bytes|json\.dump\b|pickle\.dump|\.to_csv\(|writeFile|appendFile"
    r"|createWriteStream|fs\.(?:rename|unlink|rm|copyFile|cp)|shutil\.|os\.(?:remove|rename|replace|unlink)"
    r"|writeLines|write\.(?:csv|table)|saveRDS|\bsave\(|sink\(|file\.(?:create|copy|rename|remove)|unlink\("
    r"|--inplace|fwrite|file_put_contents|>\s*['\"/]"
)
_IGNORED_TARGETS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")


def _related(a: str, b: str) -> bool:
    """One path is the other or lies inside it."""
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


@dataclass
class Effects:
    """Files a command may write (`everything`, or exact paths / directory prefixes / basenames) and
    the paths it removes (`deleted`, exact paths or directories, as of the end of the command)."""

    everything: bool = False
    paths: set[str] = field(default_factory=set)
    basenames: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)

    def merge(self, other: "Effects") -> "Effects":
        """Add the effects of `other`, which runs after these."""
        self.deleted = {d for d in self.deleted if not other.touches(d)} | other.deleted
        self.everything |= other.everything
        self.paths |= other.paths
        self.basenames |= other.basenames
        return self

    def covers(self, path: str) -> bool:
        if self.everything or posixpath.basename(path) in self.basenames:
            return True
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.paths)

    def touches(self, path: str) -> bool:
        """May write `path`, something inside it or a directory containing it."""
        if self.everything or posixpath.basename(path) in self.basenames:
            return True
        return any(_related(path, p) for p in self.paths)

    def deletes(self, path: str) -> bool:
        """`path` is gone afterwards (removed, and not written again)."""
        return not self.everything and any(path == d or path.startswith(d.rstrip("/") + "/") for d in self.deleted)

    def wrote(self, path: str | None = None, basename: str | None = None) -> None:
        if path is not None:
            self.paths.add(path)
            self.deleted = {d for d in self.deleted if not _related(d, path)}
        if basename is not None:
            self.basenames.add(basename)
            self.deleted = {d for d in self.deleted if posixpath.basename(d) != basename}


# ---- words and paths ------------------------------------------------------------------------
def _assign(words: list[str], variables: dict[str, str]) -> None:
    for w in words:
        m = _ASSIGNMENT.match(w)
        if m:
            variables[m.group(1)] = expand(m.group(3), variables)


def _add_target(eff: Effects, word: str, cwd: str | None, variables: dict[str, str]) -> None:
    w = expand(word, variables)
    if not w or w in _IGNORED_TARGETS or w.startswith(("/dev/fd/", "/proc/", "&")):
        return
    path = resolve(w, cwd)
    if w.startswith(UNRESOLVED) or path is None:  # target could be anywhere
        eff.everything = True
        return
    if not w.startswith(("/", "~")):  # relative: also match by name in case cwd tracking is off
        eff.wrote(basename=posixpath.basename(w.rstrip("/")))
    glob = _GLOB.search(path)
    if glob:
        prefix = path[: glob.start()]
        path = prefix.rstrip("/") if prefix.endswith("/") else posixpath.dirname(prefix)
    eff.wrote(path or "/")


def _add_targets(eff: Effects, prog: str, args: list[str], cwd: str | None, variables: dict[str, str]) -> None:
    """A program that writes the paths it mentions: operands, option values, paths inside program text."""
    for word in written_words(prog, args):
        _add_target(eff, word.split("=", 1)[-1] if prog == "dd" else word, cwd, variables)
        if re.search(r"\s", word):  # e.g. a SQL or sqlite dot-command text naming a file
            for m in _ABS_PATH.finditer(word):
                _add_target(eff, m.group(0), cwd, variables)
    for word in deleted_words(prog, args):
        w = expand(word, variables)
        path = None if UNRESOLVED in w else resolve(w, cwd)
        if path is not None and not _GLOB.search(path):
            eff.deleted.add(path)


def _looks_like_path(word: str) -> bool:
    return "/" in word or bool(re.search(r"\.\w{1,8}$", word))


def mentioned_paths(text: str, cwd: str | None) -> tuple[set[str], set[str]]:
    """Absolute paths a command line mentions: (exact paths, glob patterns). Best effort."""
    prep = prepare(text)
    paths: set[str] = set()
    globs: set[str] = set()
    if prep is None:
        return {m.group(0).rstrip("/") or "/" for m in _ABS_PATH.finditer(text)}, globs
    for part in [prep.main, *prep.substitutions]:
        here = cwd
        for cmd in simple_commands(part) or []:
            if cmd.words and cmd.words[0] in ("cd", "pushd"):
                arg = cmd.words[1] if len(cmd.words) > 1 else "~"
                here = None if "$" in arg else resolve(arg, here)
            for w in [*map(option_value, cmd.words), *cmd.outputs, *cmd.inputs]:
                if not w:
                    continue
                found = [w] if not re.search(r"\s", w) else []
                found += [m.group(0) for m in _ABS_PATH.finditer(w)]
                for f in found:
                    p = None if "$" in f or UNRESOLVED in f else resolve(f.rstrip("/") or "/", here)
                    if p is not None:
                        (globs if _GLOB.search(p) else paths).add(p)
    return paths, globs


# ---- classification -------------------------------------------------------------------------
def _strip_prefixes(words: list[str], variables: dict[str, str]) -> list[str]:
    """Drop leading assignments, shell keywords and wrappers like sudo/nohup/env/timeout."""
    while words:
        head = words[0]
        if _ASSIGNMENT.match(head):
            _assign([head], variables)
            words = words[1:]
        elif head in _KEYWORDS:
            words = words[1:]
        elif (
            posixpath.basename(head) in _PREFIXES
            or (head == "env" and len(words) > 1)
            or (head == "command" and len(words) > 1 and words[1] not in ("-v", "-V"))
        ):
            rest = words[1:]
            while rest and (rest[0].startswith("-") or "=" in rest[0] or re.match(r"^[\d.]+[smhd]?$", rest[0])):
                rest = rest[2:] if rest[0] in _PREFIX_ARG_OPTS and head != "env" else rest[1:]
            words = rest
        else:
            break
    return words


def _classify(prog: str, args: list[str]) -> str:
    """shell_programs.classify, plus interpreters and subcommand-based tools ("" when unknown)."""
    verdict = classify(prog, args)
    if verdict:
        return verdict
    if prog in INTERPRETERS or re.match(r"^python\d", prog):
        return READ if args and all(a in _VERSION_FLAGS for a in args) else ""
    sub = "pip" if re.fullmatch(r"pip[\d.]*", prog) else prog
    if sub in _READ_SUBCOMMANDS:
        return READ if args and args[0] in _READ_SUBCOMMANDS[sub] else ""
    if prog == "service" and args and (args[-1] == "status" or args[0] == "--status-all"):
        return READ
    return ""


def _interpreter_call(prog: str, args: list[str]) -> tuple[str, str | None]:
    """("inline", code) / ("shell_c", text) / ("script", path) / ("module", name) / ("stdin", None) /
    ("other", None): where the interpreter's program comes from."""
    shell, python = prog in SHELLS, prog.startswith("python")
    code_flags = ("-c",) if shell or python else ("-e", "--eval", "-p", "--print")
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
        elif a in code_flags and i + 1 < len(args):
            return ("shell_c" if shell else "inline"), args[i + 1]
        elif a == "-m" and python:
            return "module", args[i + 1] if i + 1 < len(args) else None
        elif a == "CMD":
            return "other", None
        elif a in ("-f", "--file") and i + 1 < len(args):
            return "script", args[i + 1]
        elif a in ("-", "-s"):
            return "stdin", None
        elif a in _INTERPRETER_VALUE_FLAGS:
            skip = True
        elif not a.startswith(("-", "+")):
            return "script", a
    return "stdin", None


@dataclass
class _Analyzer:
    read_script: Callable[[str], str | None] | None  # None = strict mode
    home: str = "/root"
    unknown_code: bool = False  # code the analysis cannot see may write anything (setup scripts)
    depth: int = 0
    seen: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=dict)  # file copied by this text -> its source
    shell_heredocs: bool = False  # a shell reads its program from a heredoc of this text

    def run(self, text: str, cwd: str | None) -> Effects:
        eff = Effects()
        prep = prepare(text)
        if prep is None or self.depth > 4:
            eff.everything = True
            return eff
        variables: dict[str, str] = {}
        for part in [*prep.substitutions, prep.main]:
            cmds = simple_commands(part)
            if cmds is None:
                eff.everything = True
                return eff
            here = cwd
            for cmd in cmds:
                here = self._command(cmd, here, variables, eff)
        if self.read_script is not None:
            for body in prep.heredocs:  # heredoc bodies fed to interpreters are code
                eff.merge(scan_code(body))
                if self.shell_heredocs:
                    eff.merge(self._nested(body, cwd))
        return eff

    def _command(self, cmd: SimpleCommand, cwd: str | None, variables: dict[str, str], eff: Effects) -> str | None:
        for target in cmd.outputs:
            _add_target(eff, target, cwd, variables)
        words = _strip_prefixes(list(cmd.words), variables)
        if not words or words[0] in _BINDINGS:
            return cwd
        prog_word, args = words[0], words[1:]
        prog = posixpath.basename(prog_word)
        if prog in ("cd", "pushd"):
            target = expand(args[0], variables) if args else self.home
            return None if UNRESOLVED in target else resolve(target, cwd, self.home)
        if prog == "popd":
            return None
        if prog_word in ("$@", "$*") and self.read_script is not None:
            return cwd  # an entrypoint handing over to CMD, which is analysed on its own
        if UNRESOLVED in expand(prog_word, variables):  # program chosen at run time
            eff.everything = True
            return cwd
        if prog in _DECLARE:
            _assign(args, variables)
            return cwd
        if prog in METADATA_ONLY:
            return cwd
        verdict = _classify(prog, args)
        if verdict == READ:
            return cwd
        if verdict == EVERYTHING:
            eff.everything = True
            return cwd
        if verdict == MENTIONED or prog in TARGETED:
            _add_targets(eff, prog, args, cwd, variables)
            pair = copied_pair(prog, args)
            if pair and self.read_script is not None:  # a script copied here can still be read
                src, dest = (resolve(expand(w, variables), cwd, self.home) for w in pair)
                if src and dest and UNRESOLVED not in src + dest:
                    self.aliases[dest] = src
                    self.aliases[posixpath.join(dest, posixpath.basename(src))] = src  # DEST was a directory
            return cwd
        if prog == "trap":
            if args and not args[0].startswith("-"):
                eff.merge(self._nested(args[0], cwd))  # code run on a signal or at exit
            return cwd
        if prog == "su" and "-c" in args[:-1]:
            eff.merge(self._nested(args[args.index("-c") + 1], cwd))
            return cwd
        if prog in BUILD_TOOLS or re.match(r"^pip\d", prog):
            eff.everything = True
            return cwd
        kind, arg = ("script", prog_word) if "/" in prog_word else ("other", None)
        if prog in ("source", "."):
            kind, arg = ("script", args[0]) if args else ("other", None)
        elif prog in INTERPRETERS or re.match(r"^python\d", prog):
            kind, arg = _interpreter_call(prog, args)
        if kind == "shell_c" and arg is not None:
            eff.merge(self._nested(arg, cwd))
            return cwd
        if self.read_script is None:  # strict mode: running code may write anywhere
            eff.everything = True
            return cwd
        self._static_code(cmd, prog, kind, arg, cwd, variables, eff)
        for a in args:  # a service or script writes at most the paths it is given
            value = option_value(a)
            if value and value != arg and _looks_like_path(value):
                _add_target(eff, value, cwd, variables)
        return cwd

    def _static_code(
        self, cmd: SimpleCommand, prog: str, kind: str, arg: str | None, cwd: str | None, variables: dict[str, str], eff: Effects
    ) -> None:
        """Static modes: what the program the command runs may write."""
        if kind == "inline" and arg is not None:
            eff.merge(scan_code(arg))
        elif kind == "script" and arg is not None:
            eff.merge(self._script(expand(arg, variables), cwd, prog))
        elif kind == "stdin":  # the program text comes from stdin
            if cmd.heredoc:  # visible: heredoc bodies are scanned in run()
                self.shell_heredocs |= prog in SHELLS
            elif cmd.inputs:
                eff.merge(self._script(expand(cmd.inputs[0], variables), cwd, prog))
            elif self.unknown_code:  # a pipe (`curl ... | bash`) or a here-string
                eff.everything = True
        elif kind == "module" and self.unknown_code:
            eff.everything = True

    def _nested(self, text: str, cwd: str | None) -> Effects:
        sub = _Analyzer(self.read_script, self.home, self.unknown_code, self.depth + 1, self.seen, self.aliases)
        return sub.run(text, cwd)

    def _script(self, word: str, cwd: str | None, prog: str) -> Effects:
        assert self.read_script is not None
        path = resolve(word, cwd, self.home) if UNRESOLVED not in word else None
        text = self.read_script(path) if path else None
        if text is None and path in self.aliases:
            text = self.read_script(self.aliases[path])
        if path is None or text is None:  # not part of the image: a system tool or service, or unseen code
            return Effects(everything=path is None or (self.unknown_code and not path.startswith(_SYSTEM_DIRS)))
        if path in self.seen:
            return Effects()
        self.seen.add(path)
        is_shell = prog in SHELLS or prog in ("source", ".") or path.endswith(".sh")
        if is_shell or re.match(r"#!.*\b(ba|da|z|k)?sh\b", text):
            return self._nested(text, cwd)
        return scan_code(text)


def scan_code(code: str) -> Effects:
    """Non-shell code (Python, R, JS, ...): if it can write files, it may write any path it names."""
    eff = Effects()
    if _WRITE_PRIMITIVE.search(code):
        eff.paths |= {posixpath.normpath(m.group(0)) for m in _ABS_PATH.finditer(code)}
        eff.basenames |= {m.group(1) for m in _QUOTED_NAME.finditer(code)}
    return eff


def command_effects(
    text: str,
    cwd: str | None,
    read_script: Callable[[str], str | None] | None = None,
    home: str = "/root",
    unknown_code: bool = False,
) -> Effects:
    """Files that running `text` (a bash command line or script) may write or delete."""
    return _Analyzer(read_script, home, unknown_code).run(text, cwd)
