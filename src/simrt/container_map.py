"""Which container files are exact copies of the environment's codebase snapshot?

`ContainerImage.load(codebase_root)` reads the `default` service of compose.yml and its Dockerfile
(final build stage) and records:
- `files`: container path -> snapshot path for files copied from the snapshot (COPY/ADD or a bind
  mount) whose content is stable: no later build step, start-up script or cron job may change it;
- `volatile`: files copied from the snapshot whose content may differ at run time;
- `deleted`: copied files (and copied directories) that a build step or start-up script removes,
  so they do not exist when the agent starts;
- `copies`: the COPY/bind mappings themselves (container -> snapshot), for lookups and the prompt;
- `workdir`, `user`, `base_image`, `startup` and `cron` commands, for the simulator's context.

It is conservative: whatever it cannot resolve is left out (the simulator model handles it).
Task setup scripts are applied with `apply_setup_script`. `lookup_snapshot` gives codebase lookups
(the simulator's and the judge's) this map.
"""

import fnmatch
import json
import logging
import posixpath
import re
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from simrt.codebase import CodebaseSnapshot
from simrt.shell_effects import Effects, command_effects
from simrt.shell_parse import UNRESOLVED, expand, resolve

logger = logging.getLogger(__name__)

_ARCHIVE = re.compile(r"\.(tar|tgz|tar\.gz|tar\.bz2|tar\.xz|txz|tbz2?)$")
_CRON_PATHS = ("/etc/cron.d/", "/etc/crontab", "/var/spool/cron/")
_OS_NAMES = {
    "jammy": "Ubuntu 22.04",
    "noble": "Ubuntu 24.04",
    "focal": "Ubuntu 20.04",
    "bookworm": "Debian 12",
    "bullseye": "Debian 11",
    "alpine": "Alpine Linux",
}


@dataclass
class ContainerImage:
    files: dict[str, str] = field(default_factory=dict)
    volatile: dict[str, str] = field(default_factory=dict)
    deleted: dict[str, str] = field(default_factory=dict)
    copies: list[tuple[str, str]] = field(default_factory=list)
    workdir: str = "/"
    user: str = "root"
    base_image: str = ""
    startup: list[str] = field(default_factory=list)
    cron: list[str] = field(default_factory=list)
    root: Path | None = None

    # ---- queries ----------------------------------------------------------------------------
    def source_of(self, path: str) -> str | None:
        """Snapshot path of a container file whose image content is exactly that snapshot file."""
        return self.files.get(path)

    def is_deleted(self, path: str) -> bool:
        return any(path == d or path.startswith(d.rstrip("/") + "/") for d in self.deleted)

    def lookup_map(self) -> tuple[tuple[str, str], ...]:
        """(container path, snapshot path) pairs for translating lookup paths, files and dirs."""
        pairs = dict(self.copies)
        pairs.update(self.volatile)
        pairs.update(self.files)
        return tuple(sorted((c, s) for c, s in pairs.items() if not self.is_deleted(c)))

    def lookup_snapshot(self, snapshot: CodebaseSnapshot) -> CodebaseSnapshot:
        """`snapshot` with this map added to its lookups, as the simulator's lookups have it: container
        paths copied from the snapshot translate to snapshot paths, those removed before the agent
        starts are reported absent, and copied files that change at run time carry a warning."""
        return replace(
            snapshot,
            container_paths=tuple(sorted({*snapshot.container_paths, *self.lookup_map()})),
            deleted=tuple(sorted({*snapshot.deleted, *self.deleted.items()})),
            volatile=snapshot.volatile | frozenset(self.volatile),
        )

    def os_description(self) -> str:
        image = self.base_image or "unknown"
        for key, name in _OS_NAMES.items():
            if key in image:
                return f"{name} (base image {image})"
        m = re.match(r"^(ubuntu|debian):(\d[\d.]*)", image)
        if m:
            return f"{m.group(1).capitalize()} {m.group(2)} (base image {image})"
        return f"base image {image} (OS release not stated in the Dockerfile)"

    def read_snapshot(self, path: str) -> str | None:
        """Text of the snapshot file a container path was copied from (stable or not)."""
        rel = self.files.get(path) or self.volatile.get(path)
        if rel is None or self.root is None:
            return None
        try:
            return (self.root / rel).read_text(errors="replace")
        except OSError:
            return None

    # ---- construction -----------------------------------------------------------------------
    @classmethod
    def load(cls, root: Path) -> "ContainerImage":
        img = cls(root=root.resolve())
        try:
            img._load()
        except Exception as e:  # noqa: BLE001 - a malformed env must not break simulation
            logger.warning("container map for %s failed: %s", root, e)
            return cls(root=root.resolve())
        return img

    def _load(self) -> None:
        assert self.root is not None
        compose = self._yaml("compose.yml") or self._yaml("docker-compose.yml") or {}
        svc: dict[str, Any] = (compose.get("services") or {}).get("default") or {}
        build = svc.get("build", {} if not svc.get("image") else None)
        context, dockerfile = ".", "Dockerfile"
        if isinstance(build, str):
            context = build
        elif isinstance(build, dict):
            context, dockerfile = build.get("context", "."), build.get("dockerfile", "Dockerfile")
        ctx = (self.root / context).resolve()
        entrypoint: str | None = None
        cmd: str | None = None
        if build is not None and _inside(ctx, self.root) and (ctx / dockerfile).is_file():
            entrypoint, cmd = self._dockerfile((ctx / dockerfile).read_text(errors="replace"), ctx)
        elif svc.get("image"):
            self.base_image = str(svc["image"])
        self.workdir = str(svc.get("working_dir") or self.workdir)
        self.user = str(svc.get("user") or self.user).split(":")[0]
        if svc.get("entrypoint") is not None:  # compose entrypoint also clears the image's CMD
            entrypoint, cmd = _command_text(svc["entrypoint"]), None
        if svc.get("command") is not None:
            cmd = _command_text(svc["command"])
        self.startup = [c for c in (entrypoint, cmd) if c]
        for vol in svc.get("volumes") or []:
            self._volume(vol)
        self._runtime_effects()

    def _yaml(self, name: str) -> dict[str, Any] | None:
        assert self.root is not None
        path = self.root / name
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else None

    def _dockerfile(self, text: str, ctx: Path) -> tuple[str | None, str | None]:
        """Apply the final build stage; returns its (ENTRYPOINT, CMD)."""
        env: dict[str, str] = {}
        ignored = _dockerignore(ctx)
        entrypoint: str | None = None
        cmd: str | None = None
        for instr, arg in _instructions(text):
            if instr == "FROM":
                words = [w for w in arg.split() if not w.startswith("--")]
                self.base_image = words[0] if words else ""
                self.files, self.volatile, self.deleted, self.copies = {}, {}, {}, []
                self.workdir, self.user, entrypoint, cmd = "/", "root", None, None
            elif instr in ("ENV", "ARG"):
                _record_env(arg, env)
            elif instr == "WORKDIR":
                self.workdir = resolve(expand(arg.strip(), env), self.workdir) or self.workdir
            elif instr == "USER":
                self.user = expand(arg.strip(), env).split(":")[0]
            elif instr in ("COPY", "ADD"):
                self._copy(arg, ctx, env, ignored, is_add=instr == "ADD")
            elif instr == "RUN":
                self._apply(command_effects(_exec_form(arg), self.workdir, self.read_snapshot))
            elif instr == "ENTRYPOINT":
                entrypoint, cmd = _exec_form(arg), None
            elif instr == "CMD":
                cmd = _exec_form(arg)
        return entrypoint, cmd

    def _copy(self, arg: str, ctx: Path, env: dict[str, str], ignored: list[str], is_add: bool) -> None:
        assert self.root is not None
        words = json.loads(arg) if arg.lstrip().startswith("[") else shlex.split(arg)
        flags = [w for w in words if w.startswith("--")]
        words = [expand(w, env) for w in words if not w.startswith("--")]
        if len(words) < 2:
            return
        *sources, dest_word = words
        dest = resolve(dest_word, self.workdir) if UNRESOLVED not in dest_word else None
        if dest is None:
            return
        foreign = any(f.startswith(("--from", "--parents", "--exclude")) for f in flags) or (
            is_add and any("://" in s or _ARCHIVE.search(s) for s in sources)
        )  # content not from the snapshot, or laid out in ways not modelled here
        if foreign or any(UNRESOLVED in s for s in sources):
            self._apply(Effects(paths={dest}))
            return
        into_dir = (
            dest_word.endswith("/")
            or len(sources) > 1
            or dest == self.workdir
            or any(p.startswith(dest.rstrip("/") + "/") for p in [*self.files, *self.volatile])
        )
        for source in sources:
            pattern = source[2:] if source.startswith("./") else source
            matches = sorted(ctx.glob(pattern)) if re.search(r"[*?\[]", pattern) else [ctx / pattern]
            for match in matches:
                if not match.exists() or not _inside(match.resolve(), ctx):
                    continue
                if match.is_dir():
                    self._copied(dest, _rel(match, self.root))
                    for f in sorted(match.rglob("*")):
                        self._set(posixpath.join(dest, f.relative_to(match).as_posix()), f, ctx, ignored)
                else:
                    target = posixpath.join(dest, match.name) if into_dir or len(matches) > 1 else dest
                    self._copied(target, _rel(match, self.root))
                    self._set(target, match, ctx, ignored)

    def _copied(self, target: str, snap: str) -> None:
        self.copies.append((target, snap))
        for d in [d for d in self.deleted if target == d or target.startswith(d.rstrip("/") + "/")]:
            del self.deleted[d]  # copied (back) after a removal

    def _set(self, target: str, src: Path, ctx: Path, ignored: list[str]) -> None:
        assert self.root is not None
        if src.is_symlink() or not src.is_file():
            return
        rel_ctx = src.relative_to(ctx).as_posix()
        self.volatile.pop(target, None)
        self.deleted.pop(target, None)
        if any(fnmatch.fnmatch(rel_ctx, p) or rel_ctx.startswith(p.rstrip("/") + "/") for p in ignored):
            self.files.pop(target, None)  # maybe excluded by .dockerignore: leave it to the model
            return
        self.files[target] = _rel(src, self.root)

    def _volume(self, vol: Any) -> None:
        assert self.root is not None
        if isinstance(vol, str):
            parts = vol.split(":")
            source, target = (parts[0], parts[1]) if len(parts) > 1 else (None, parts[0])
        elif isinstance(vol, dict):
            source, target = vol.get("source"), vol.get("target")
        else:
            return
        if not target:
            return
        target = resolve(str(target), "/") or "/"
        host = (self.root / source).resolve() if source and str(source).startswith((".", "/")) else None
        self._apply(Effects(paths={target}))  # whatever the image had there is shadowed
        if host is None or not _inside(host, self.root) or not host.exists():
            return
        self._copied(target, _rel(host, self.root))
        if host.is_file():
            self._set(target, host, self.root, [])
        else:
            for f in sorted(host.rglob("*")):
                self._set(posixpath.join(target, f.relative_to(host).as_posix()), f, self.root, [])

    def _runtime_effects(self) -> None:
        """Start-up commands run before the agent starts and cron jobs while it works: what they write
        is volatile; what start-up removes is deleted (a cron job's removal recurs, so only volatile)."""
        for command in self.startup:
            self._apply(command_effects(command, self.workdir, self.read_snapshot))
        for path in sorted({*self.files, *self.volatile}):
            if path.startswith(_CRON_PATHS):
                system = not path.startswith("/var/spool")
                for schedule, command in _cron_lines(self.read_snapshot(path) or "", system):
                    self.cron.append(f"{schedule}  {command}")
                    self._apply(command_effects(command, self.workdir, self.read_snapshot), deletions=False)

    def runtime_volatile(self, script: str) -> Effects:
        """Effects of a task setup script: analysed like the start-up scripts, except that code the
        analysis cannot see (a script not in the image, `curl ... | bash`) may write anything."""
        return command_effects(script, self.workdir, self.read_snapshot, unknown_code=True)

    def apply_setup_script(self, script: str) -> Effects:
        """A setup script ran (as root, before the agent started): update the map; returns its effects."""
        eff = self.runtime_volatile(script)
        self._apply(eff)
        return eff

    def _apply(self, eff: Effects, deletions: bool = True) -> None:
        """Copied files the effects may change become volatile; those they remove become deleted."""
        for path in [p for p in self.files if eff.covers(p)]:
            self.volatile[path] = self.files.pop(path)
        for path in [p for p in self.deleted if eff.covers(p) and not eff.deletes(p)]:
            self.volatile[path] = self.deleted.pop(path)  # written again: it exists, content unknown
        if not deletions:
            return
        for path in [p for p in self.volatile if eff.deletes(p)]:
            self.deleted[path] = self.volatile.pop(path)
        for container, snap in self.copies:
            if eff.deletes(container):
                self.deleted.setdefault(container, snap)


# ---- helpers ----------------------------------------------------------------------------------
def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix() if path.resolve() != root else "."


def _exec_form(arg: str) -> str:
    arg = arg.strip()
    if arg.startswith("["):
        try:
            return shlex.join(str(w) for w in json.loads(arg))
        except (ValueError, TypeError):
            return arg
    return arg


def _command_text(value: Any) -> str:
    return shlex.join(str(w) for w in value) if isinstance(value, list) else str(value)


def _record_env(arg: str, env: dict[str, str]) -> None:
    try:
        words = shlex.split(arg)
    except ValueError:
        return
    if len(words) >= 2 and "=" not in words[0]:
        env[words[0]] = " ".join(words[1:])  # legacy `ENV KEY value`
        return
    for w in words:
        key, sep, value = w.partition("=")
        if sep:
            env[key] = expand(value, env)


def _instructions(text: str) -> list[tuple[str, str]]:
    """Dockerfile -> [(INSTRUCTION, arguments)], with continuations and RUN heredocs joined."""
    out: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [line.rstrip()]
        while parts[-1].endswith("\\") and i < len(lines):
            parts[-1] = parts[-1][:-1]
            nxt = lines[i]
            i += 1
            if not nxt.strip() or nxt.lstrip().startswith("#"):  # skipped inside continuations
                parts.append("\\")
                continue
            parts.append(nxt.rstrip())
        joined = " ".join(p for p in parts if p != "\\")
        for delim in re.findall(r"(?<!<)<<(?!<)-?\s*['\"]?(\w+)['\"]?", joined):
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != delim:
                body.append(lines[i])
                i += 1
            i += 1
            joined += "\n" + "\n".join(body) + "\n" + delim
        instr, _, arg = joined.strip().partition(" ")
        out.append((instr.upper(), arg))
    return out


def _dockerignore(ctx: Path) -> list[str]:
    path = ctx / ".dockerignore"
    if not path.is_file():
        return []
    pats = [ln.strip().lstrip("!").lstrip("/") for ln in path.read_text().splitlines()]
    return [p.replace("**/", "*").replace("/**", "/*") for p in pats if p and not p.startswith("#")]


def _cron_lines(text: str, system: bool) -> list[tuple[str | None, str]]:
    """Crontab -> [(schedule, command)]; system crontabs have a user field after the schedule."""
    out: list[tuple[str | None, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or re.match(r"^[A-Za-z_]\w*\s*=", line):
            continue
        fields = line.split(None, 1 if line.startswith("@") else 5)
        n_sched = 1 if line.startswith("@") else 5
        if len(fields) <= n_sched:
            continue
        rest = fields[n_sched]
        if system:
            user, _, rest = rest.partition(" ")
            schedule = " ".join(fields[:n_sched]) + f" (as {user})"
        else:
            schedule = " ".join(fields[:n_sched])
        out.append((schedule, rest.strip()))
    return out
