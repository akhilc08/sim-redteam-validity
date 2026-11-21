"""Run manifest: tie a run directory to the code, inputs and images it was produced with.

Writes DIR/manifest.json (the latest record) and appends the same record to DIR/manifests.jsonl, so
a resumed or repeated stage keeps the history of what each invocation ran with. A record holds:
- the simrt commit, dirty flag and changed paths, and the control-tower, linuxarena-paper and
  environment repo revisions (found from the running interpreter, not from hard-coded paths); for a
  dirty checkout also changes_sha256, a hash of `git diff HEAD --binary` and of every untracked,
  non-ignored file, so two runs with different uncommitted code get different records;
- Python, inspect_ai and control-tower versions;
- Docker image ids and digests (LinuxArena images, the internet-simulator image and the running
  simulator container), or null when Docker is unavailable;
- sha256 of the config files used, run parameters, the command line and a UTC timestamp;
- selected environment variables (image tags, cache switch). For provider API keys only whether
  they are set in the environment of this process is recorded, never a value;
- inventory: the files already in DIR when the record was written (size, mtime, and the number of
  complete lines of a .jsonl file), leaving out subdirectories with their own manifest.json. A record
  is written when a stage starts, also when the stage then finds nothing to do. Whatever is in DIR
  but not in a record's inventory (a file, or .jsonl lines past the recorded count) was written after
  that record: by that invocation or a later one, or by another stage writing the same directory.

  python -m simrt.manifest --out DIR [--config FILE ...] [--param KEY=VALUE ...] [-- COMMAND ...]
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Runner = Callable[[Sequence[str]], str | None]

PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
RECORDED_ENV = ("AWS_DEFAULT_REGION", "SKIP_DOCKER_BUILD", "INTERNET_SIMULATOR_IMAGE",
                "SIMRT_POLICY_CACHE", "SIMRT_KEEP_KEYS")
RECORDED_ENV_PREFIX = "ENV_IMAGE_TAG_"
SIM_IMAGE_DEFAULT = "linuxarena/env-internet-simulator:latest"  # Control Tower's default
SIM_CONTAINER = "internet-simulator"
MAX_CHANGED = 50
MAX_INVENTORY = 5000  # files listed per record; more and the inventory says truncated
OWN_FILES = ("manifest.json", "manifests.jsonl")


def run_cmd(cmd: Sequence[str]) -> str | None:
    """stdout of a command (trailing newlines removed), or None if it is missing, fails or hangs.
    Bytes that are not UTF-8 (e.g. in a diff) survive as surrogates: encode with surrogateescape."""
    try:
        res = subprocess.run(list(cmd), capture_output=True, encoding="utf-8", errors="surrogateescape",
                             timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.rstrip("\n") if res.returncode == 0 else None


def changes_sha256(top: str, run: Runner = run_cmd) -> str | None:
    """sha256 over `git diff HEAD --binary` and each untracked, non-ignored file (path and content):
    the same for the same uncommitted code on the same commit."""
    diff = run(["git", "--no-optional-locks", "-C", top, "diff", "HEAD", "--binary"])
    others = run(["git", "--no-optional-locks", "-C", top, "ls-files", "--others", "--exclude-standard", "-z"])
    if diff is None or others is None:
        return None
    h = hashlib.sha256(diff.encode("utf-8", "surrogateescape"))
    for rel in sorted(p for p in others.split("\0") if p):
        h.update(b"\0" + rel.encode("utf-8", "surrogateescape") + b"\0")
        try:
            with open(Path(top, rel), "rb") as f:
                h.update(hashlib.file_digest(f, "sha256").digest())
        except OSError:
            h.update(b"(unreadable)")
    return h.hexdigest()


def git_state(path: Path | None, run: Runner = run_cmd) -> dict[str, Any] | None:
    """Commit, exact tag, dirty flag, changed paths and a hash of the changes of the git checkout
    containing `path`."""
    if path is None:
        return None
    top = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if not top:
        return None
    status = run(["git", "--no-optional-locks", "-C", top, "status", "--porcelain"])
    changed = [line[3:] for line in (status or "").splitlines() if line.strip()]
    return {
        "path": top,
        "commit": run(["git", "-C", top, "rev-parse", "HEAD"]),
        "tag": run(["git", "-C", top, "describe", "--tags", "--exact-match"]),
        "dirty": None if status is None else bool(changed),
        "n_changed": len(changed),
        "changed": changed[:MAX_CHANGED],  # first MAX_CHANGED paths
        "changes_sha256": changes_sha256(top, run) if changed else None,
    }


def locate_repos() -> dict[str, Path | None]:
    """Checkouts behind the running code: simrt (this file), control-tower (the imported package)
    and linuxarena-paper (the checkout holding this interpreter's venv)."""
    ct = importlib.util.find_spec("control_tower")
    return {
        "simrt": Path(__file__).resolve().parent,
        "control-tower": Path(ct.origin).resolve().parent if ct and ct.origin else None,
        "linuxarena-paper": Path(sys.prefix).resolve(),
    }


def environment_states(ct_top: str | None, run: Runner = run_cmd) -> dict[str, dict[str, Any] | None]:
    """Revision of every environment repo cloned under control-tower/.environments."""
    envs = Path(ct_top) / ".environments" if ct_top else None
    if envs is None or not envs.is_dir():
        return {}
    # An env dir without its own .git would resolve to control-tower's repo, so skip git for it.
    return {d.name: git_state(d, run) if (d / ".git").exists() else None
            for d in sorted(envs.iterdir()) if d.is_dir()}


def versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {"python": platform.python_version()}
    for dist in ("inspect_ai", "control-tower", "simrt"):
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = None
    return out


def _json(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return None


def docker_images(environ: Mapping[str, str], run: Runner = run_cmd) -> dict[str, Any] | None:
    """Local LinuxArena images with digests, the configured internet-simulator image and the image
    of the running simulator container. None when Docker is unavailable."""
    listing = run(["docker", "image", "ls", "--digests", "--no-trunc", "--format", "{{json .}}",
                   "--filter", "reference=linuxarena/*"])
    if listing is None:
        return None
    images = []
    for line in listing.splitlines():
        d = _json(line)
        if isinstance(d, dict):
            images.append({k.lower(): d.get(k) for k in ("Repository", "Tag", "Digest", "ID")})
    sim_ref = environ.get("INTERNET_SIMULATOR_IMAGE") or SIM_IMAGE_DEFAULT
    return {
        "images": images,
        "internet_simulator_ref": sim_ref,
        "internet_simulator_repo_digests": _json(
            run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", sim_ref])),
        "internet_simulator_container_image": run(
            ["docker", "inspect", "--format", "{{.Image}}", SIM_CONTAINER]),
    }


def config_hashes(paths: Sequence[str]) -> list[dict[str, str]]:
    out = []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            raise SystemExit(f"manifest: config file not found: {p}")
        out.append({"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return out


def env_snapshot(environ: Mapping[str, str]) -> dict[str, Any]:
    recorded = {k: v for k, v in environ.items() if k in RECORDED_ENV or k.startswith(RECORDED_ENV_PREFIX)}
    return {"vars": dict(sorted(recorded.items())),
            "provider_keys_set": {k: bool(environ.get(k)) for k in PROVIDER_KEYS}}


def _file_entry(p: Path) -> dict[str, Any] | None:
    try:
        st = p.stat()
        lines = p.read_bytes().count(b"\n") if p.suffix == ".jsonl" else None
    except OSError:  # removed meanwhile
        return None
    entry: dict[str, Any] = {"bytes": st.st_size,
                             "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds")}
    if lines is not None:
        entry["lines"] = lines
    return entry


def inventory(out_dir: Path) -> dict[str, Any]:
    """The files already under out_dir, except this module's own and subdirectories that hold their own
    manifest.json: {"files": {relative path: {"bytes", "mtime"[, "lines"]}}, "truncated": bool}."""
    files: dict[str, dict[str, Any]] = {}
    for root, dirs, names in os.walk(out_dir):
        here = Path(root)
        dirs[:] = sorted(d for d in dirs if not (here / d / "manifest.json").exists())
        for name in sorted(names):
            if here == out_dir and (name in OWN_FILES or name.startswith(".manifest.")):
                continue
            if len(files) >= MAX_INVENTORY:
                return {"files": files, "truncated": True}
            if (entry := _file_entry(here / name)) is not None:
                files[str((here / name).relative_to(out_dir))] = entry
    return {"files": files, "truncated": False}


def build_manifest(
    configs: Sequence[str],
    params: Mapping[str, str],
    argv: Sequence[str],
    *,
    run: Runner | None = None,
    repos: Mapping[str, Path | None] | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    run = run_cmd if run is None else run
    environ = os.environ if environ is None else environ
    states = {name: git_state(path, run) for name, path in (locate_repos() if repos is None else repos).items()}
    return {
        "created_utc": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "argv": list(argv),
        "cwd": os.getcwd(),
        "params": dict(params),
        "repos": states,
        "environments": environment_states((states.get("control-tower") or {}).get("path"), run),
        "versions": versions(),
        "docker": docker_images(environ, run),
        "configs": config_hashes(configs),
        "env": env_snapshot(environ),
        "inventory": inventory(out_dir) if out_dir is not None else None,
    }


def write_manifest(out_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """Write out_dir/manifest.json (atomically) and append the record to out_dir/manifests.jsonl."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest.json"
    tmp = out_dir / f".manifest.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)
    # One O_APPEND write per record, so concurrent stages cannot interleave lines.
    fd = os.open(out_dir / "manifests.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(manifest, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    return path


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="run directory to write manifest.json into")
    ap.add_argument("--config", action="append", default=[], help="config file the run uses (hashed)")
    ap.add_argument("--param", action="append", default=[], help="KEY=VALUE run parameter to record")
    ap.add_argument("command", nargs="*", help="command line being recorded (put it after --)")
    a = ap.parse_args(argv)
    params: dict[str, str] = {}
    for p in a.param:
        k, sep, v = p.partition("=")
        if not sep or not k:
            ap.error(f"bad --param (want KEY=VALUE): {p}")
        params[k] = v
    command = a.command or [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    path = write_manifest(Path(a.out), build_manifest(a.config, params, command, out_dir=Path(a.out)))
    print(f"manifest: {path}")


if __name__ == "__main__":
    main()
