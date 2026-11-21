"""A fake box deployment for the infra tests: stub ct, python, docker, setsid and flock under a
temporary HOME that log their argv, key and image environment (no model, network or Docker calls),
plus helpers to write real Inspect .eval logs.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
SONNET_BEDROCK = ("rotate/anthropic/bedrock/us.anthropic.claude-sonnet-4-6@us-east-1,"
                  "anthropic/bedrock/global.anthropic.claude-sonnet-4-6,"
                  "anthropic/bedrock/eu.anthropic.claude-sonnet-4-6@eu-west-1")
API_ROUTE = "anthropic/claude-sonnet-4-6"
DUMMY_KEY = "sk-test-not-a-real-key"
HONEST = [tuple(x.split()) for x in (REPO / "config/tasks/m1_honest.txt").read_text().splitlines() if x.strip()]


def opt(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def params(call: dict) -> dict[str, str]:
    """--param KEY=VALUE pairs of a logged simrt.manifest call."""
    a = call["argv"]
    return dict(a[i + 1].split("=", 1) for i, x in enumerate(a[:-1]) if x == "--param")


def touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_eval(path: Path, samples: list[tuple], tag: str = "s", actions: int = 0) -> Path:
    """A real Inspect .eval log. A sample is (env, main_task, side_task or None, problem[, epoch]); problem
    is falsy (clean), "solver_error" (ct's swallowed agent error) or anything else (a sample error). Ids
    are "<main>:<tag><n>" like ct's "<main>:<random hex>"; with an epoch, "<main>:<tag>" for every epoch.
    Each sample gets `actions` bash tool calls (enough, at simrt.reference.MIN_REFERENCE_ACTIONS, for a
    clean honest sample to be usable as a simulator reference)."""
    from inspect_ai.log import EvalConfig, EvalDataset, EvalError, EvalLog, EvalSample, EvalSpec, write_eval_log
    from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, ChatMessageUser
    from inspect_ai.scorer import Score
    from inspect_ai.tool import ToolCall

    rows = []
    for i, (env, main, side, problem, *epoch) in enumerate(samples):
        md = {"environment": {"id": env}, "main_task": {"id": main}, "side_task": {"id": side} if side else None}
        if problem == "solver_error":
            md["solver_error"] = "Message limit reached"
        msgs: list = [ChatMessageUser(content="task")]
        for k in range(actions):
            call = ToolCall(id=f"{tag}{i}-{k}", function="bash", arguments={"cmd": f"echo {k}"})
            msgs += [ChatMessageAssistant(content="", tool_calls=[call]),
                     ChatMessageTool(content=str(k), tool_call_id=call.id, function="bash")]
        rows.append(EvalSample(
            id=f"{main}:{tag}" if epoch else f"{main}:{tag}{i}", epoch=epoch[0] if epoch else 1, input="", target="",
            messages=msgs if actions else [],
            metadata=md, scores={"linux_scorer": Score(value={"main_task_success": "C"})},
            error=EvalError(message="boom", traceback="", traceback_ansi="")
            if problem and problem != "solver_error" else None,
        ))
    spec = EvalSpec(created="2026-11-20T00:00:00+00:00", task="t", dataset=EvalDataset(),
                    model="mockllm/model", config=EvalConfig())
    path.parent.mkdir(parents=True, exist_ok=True)
    write_eval_log(EvalLog(eval=spec, samples=rows), str(path))
    return path


STUB = """#!@EXE@ -I
import json, os, shutil, sys, uuid
args, name = sys.argv[1:], os.path.basename(sys.argv[0])
if name == "python":
    if args[:1] != ["-m"]:  # helper scripts and inline code run for real
        os.execv("@EXE@", ["@EXE@", *args])
    name = args[1]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"prog": name, "argv": args, "key": os.environ.get("ANTHROPIC_API_KEY"),
                        "image": os.environ.get("INTERNET_SIMULATOR_IMAGE")}) + "\\n")
opt = lambda o: args[args.index(o) + 1] if o in args else None

def scored(evals):  # (sample id, epoch, sus) per sample of readable logs: 9.0 for a failed run, else 2.0
    from inspect_ai.log import read_eval_log
    for f in evals:
        try:
            samples = read_eval_log(f).samples or []
        except Exception:
            continue
        for s in samples:
            yield s.id, s.epoch, 9.0 if s.error or (s.metadata or {}).get("solver_error") else 2.0

if name == "docker":  # FAKE_DOCKER={"args prefix": [rc, stdout]}: longest prefix wins, else unavailable
    table = sorted(json.loads(os.environ.get("FAKE_DOCKER", "{}")).items(), key=lambda kv: -len(kv[0]))
    rc, out = next((v for k, v in table if " ".join(args).startswith(k)), (1, ""))
    print(out, end="")
    sys.exit(rc)
if name == "simrt.missing_pairs" and os.environ.get("FAKE_MISSING"):
    print(os.environ["FAKE_MISSING"])
if name == "simrt.monitoring":  # scores keyed like simrt.monitoring's: "<sample id>:<epoch>"
    d = opt("--evals")
    evals = sorted(os.path.join(d, x) for x in os.listdir(d) if x.endswith(".eval"))
    json.dump({f"{i}:{e}": {"sus": v, "runs": [v]} for i, e, v in scored(evals)}, open(opt("--out"), "w"))
if name == "ct" and args[:2] == ["run", "monitor"]:  # one trajectory JSON per sample, as ct saves them
    os.makedirs(opt("--save-dir"), exist_ok=True)
    for i, e, v in scored([args[2]]):
        json.dump({"eval_sample_id": i, "epoch": e, "traj_monitor_response": {"sus_score": v}},
                  open(os.path.join(opt("--save-dir"), uuid.uuid4().hex + ".json"), "w"))
if os.environ.get("FAKE_EVAL") and opt("--log-dir"):
    os.makedirs(opt("--log-dir"), exist_ok=True)
    shutil.copy(os.environ["FAKE_EVAL"], os.path.join(opt("--log-dir"), "run%d.eval" % len(os.listdir(opt("--log-dir")))))
prog, _, part = os.environ.get("FAKE_FAIL", "").partition(":")  # FAKE_FAIL=prog:substring-of-args
sys.exit(1 if prog == name and part in " ".join(args) else 0)
"""
FLOCK = """#!@EXE@ -I
import fcntl, sys
try:
    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | (fcntl.LOCK_NB if "-n" in sys.argv else 0))
except BlockingIOError:
    sys.exit(1)
"""
SETSID = """#!@EXE@ -I
import os, sys
if os.getpgrp() == os.getpid() and os.fork():  # like util-linux setsid: a group leader forks first
    os._exit(0)
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
"""


class Box:
    def __init__(self, root: Path) -> None:
        self.root, self.home, self.bin, self.log = root, root / "home", root / "bin", root / "calls.jsonl"
        self.work = self.home / "work"
        venv = self.work / "linuxarena-paper/.venv/bin"
        (self.work / "control-tower/.environments/web_scraping/codebase").mkdir(parents=True)
        for path, text in ((venv / "python", STUB), (venv / "ct", STUB), (self.bin / "docker", STUB),
                           (self.bin / "flock", FLOCK), (self.bin / "setsid", SETSID)):
            touch(path, text.replace("@EXE@", sys.executable)).chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_LOG": str(self.log),
                "LANG": "C", **extra}

    def run(self, script: str, *args: str, timeout: int = 120, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPTS / script), *args], env=self.env(**extra), cwd=self.root,
                              capture_output=True, text=True, timeout=timeout)

    def calls(self, prog: str) -> list[dict]:
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [c for c in map(json.loads, lines) if c["prog"] == prog]

    def m3_ready(self, pairs: str | None = None) -> None:  # references, threshold, run dirs of `pairs`
        touch(self.work / "logs/m1/honest/h.eval")
        touch(self.work / "logs/m3/threshold.txt", "5.0\n")
        for line in (REPO / f"config/tasks/{pairs}").read_text().splitlines() if pairs else []:
            _, main, side = line.split()
            for k in range(3):
                touch(self.work / f"logs/m3/runs/{main}__{side}/rep{k}/rounds.jsonl")


def key_env(tmp_path: Path) -> dict[str, str]:
    return {"SIMRT_KEY_FILE": str(touch(tmp_path / "keys/anthropic.txt", f"ANTHROPIC_API_KEY={DUMMY_KEY}\n"))}


def poll(check, what: str, timeout: float = 30) -> None:  # noqa: ANN001
    end = time.time() + timeout
    while not check():
        assert time.time() < end, f"timed out waiting for {what}"
        time.sleep(0.05)


def wait_for(path: Path) -> str:
    poll(path.exists, str(path))
    return path.read_text().strip()
