"""The honest stage and the reference loader agree on which runs can be simulator references.

scripts/missing_honest.py --reference (and box_env.sh's honest_until_complete --reference) list a task
until one of its runs is usable by simrt.reference, with the loader's own rule
(simrt.reference.reference_problem), and never change the --select selection behind the threshold.
No model, network or Docker calls: box_env.sh runs against tests/boxfakes.py's fake deployment.
"""

import os
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from boxfakes import SCRIPTS, Box, touch
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalError,
    EvalLog,
    EvalSample,
    EvalSampleLimit,
    EvalSpec,
    read_eval_log,
    write_eval_log,
)
from inspect_ai.model import ChatMessage, ChatMessageAssistant, ChatMessageTool, ChatMessageUser
from inspect_ai.scorer import Score
from inspect_ai.tool import ToolCall

import simrt.reference as reference

missing = SimpleNamespace(**runpy.run_path(str(SCRIPTS / "missing_honest.py")))  # no scripts/__pycache__
N = reference.MIN_REFERENCE_ACTIONS
PASS = {"linux_scorer": Score(value={"main_task_success": "C"})}
LIMIT = EvalSampleLimit(type="message", limit=10)
ERR = EvalError(message="boom", traceback="", traceback_ansi="")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SIMRT_ALLOW_MISSING_REFERENCE", "SIMRT_LEGACY_TRANSCRIPTS"):
        monkeypatch.delenv(var, raising=False)
    reference._candidates.cache_clear()


def honest(sid: str, main: str, actions: int, md: dict | None = None, **fields: object) -> EvalSample:
    """An honest sample of task `e <main>` with `actions` tool calls, scored (passed) unless scores=None."""
    msgs: list[ChatMessage] = [ChatMessageUser(content="task")]
    for i in range(actions):
        call = ToolCall(id=f"{sid}-{i}", function="bash", arguments={"cmd": f"echo {i}"})
        msgs += [ChatMessageAssistant(content="", tool_calls=[call]),
                 ChatMessageTool(content=str(i), tool_call_id=call.id, function="bash")]
    meta = {"environment": {"id": "e"}, "main_task": {"id": main}, "side_task": None} | (md or {})
    return EvalSample(id=sid, epoch=1, input="", target="", messages=msgs, metadata=meta,
                      **({"scores": PASS} | fields))


def write_log(path: Path, samples: list[EvalSample]) -> Path:
    spec = EvalSpec(created="2026-11-20T00:00:00+00:00", task="t", dataset=EvalDataset(), model="none/none",
                    config=EvalConfig())
    path.parent.mkdir(parents=True, exist_ok=True)
    write_eval_log(EvalLog(eval=spec, samples=samples), str(path))
    return path


def loader_accepts(ref_dir: Path, main: str) -> bool:
    reference._candidates.cache_clear()
    try:
        reference.select_reference(str(ref_dir), main)
    except reference.ReferenceUnavailable:
        return False
    return True


def pass1(path: Path) -> Path:
    """a: clean but N-1 tool calls; b: clean and usable; c: clean but stopped by a limit; d: unscored (not
    clean) but usable; f: errored."""
    return write_log(path, [honest("a:p1", "a", N - 1), honest("b:p1", "b", N),
                            honest("c:p1", "c", N + 4, limit=LIMIT), honest("d:p1", "d", N + 4, scores=None),
                            honest("f:p1", "f", N + 4, error=ERR)])


def test_reference_problem_is_the_loaders_rule(tmp_path):
    cases = {"ok": honest("ok", "t_ok", N), "unscored": honest("unscored", "t_unscored", N, scores=None),
             "short": honest("short", "t_short", N - 1), "limited": honest("limited", "t_limited", N + 4, limit=LIMIT),
             "errored": honest("errored", "t_errored", N + 4, error=ERR),
             "solver": honest("solver", "t_solver", N + 4, md={"solver_error": "Message limit reached"}),
             "attack": honest("attack", "t_attack", N + 4, md={"side_task": {"id": "st"}})}
    log = write_log(tmp_path / "a.eval", list(cases.values()))
    for s in read_eval_log(str(log)).samples or []:
        problem = reference.reference_problem(s)
        assert (problem is None) == (s.id in ("ok", "unscored")), (s.id, problem)
        assert loader_accepts(tmp_path, f"t_{s.id}") == (problem is None), s.id
    assert reference.reference_problem(cases["short"]) == f"only {N - 1} actions"


def test_missing_honest_reruns_tasks_without_a_usable_reference_and_keeps_the_selection(tmp_path, capsys):
    d, tasks = tmp_path / "honest", touch(tmp_path / "t.txt", "e a\ne b\ne c\ne d\ne f\n")
    pass1(d / "2026-11-20T01-00-00_pass1.eval")
    selected = {"e a": ["a:p1:1"], "e b": ["b:p1:1"], "e c": ["c:p1:1"], "e d": [], "e f": []}
    assert missing.selection(tasks, d, 1) == (selected, ["e d", "e f"])  # the threshold's rule, as before
    assert missing.missing(tasks, d, 1) == ["e d", "e f"]
    # a and c have their clean run, but the loader would refuse them: the honest stage must rerun them
    assert not loader_accepts(d, "a") and not loader_accepts(d, "c")
    rerun = missing.missing(tasks, d, 1, reference=True)
    assert rerun == ["e a", "e c", "e d", "e f"]
    for line in missing.task_lines(tasks):  # whatever the stage would not rerun, the loader can use
        assert line in rerun or loader_accepts(d, line.split()[1]), line
    missing.main(["--task-file", str(tasks), "--evals", str(d), "--epochs", "1", "--reference"])
    out, err = capsys.readouterr()
    assert out == "e a\ne c\ne d\ne f\n"
    assert ("e a: no run usable as a simulator reference: "
            f"2026-11-20T01-00-00_pass1.eval a:p1:1: only {N - 1} actions") in err
    assert "e c: no run usable" in err and "stopped by the message limit" in err and "e d:" not in err
    # a rerun of a with enough tool calls: a is done, and the selection still holds its first clean run
    write_log(d / "2026-11-20T02-00-00_pass2.eval", [honest("a:p2", "a", N)])
    assert missing.missing(tasks, d, 1, reference=True) == ["e c", "e d", "e f"] and loader_accepts(d, "a")
    assert missing.selection(tasks, d, 1)[0] == selected
    with pytest.raises(SystemExit):  # --reference never reaches the selection
        missing.main(["--task-file", str(tasks), "--evals", str(d), "--epochs", "1", "--select",
                      str(tmp_path / "sel.json"), "--reference"])
    assert not (tmp_path / "sel.json").exists()


def test_the_rule_comes_from_simrt_reference(tmp_path, monkeypatch):
    d, tasks = tmp_path / "honest", touch(tmp_path / "t.txt", "e a\ne b\n")
    pass1(d / "2026-11-20T01-00-00_pass1.eval")
    assert missing.missing(tasks, d, 1, reference=True) == ["e a"] and not loader_accepts(d, "a")
    monkeypatch.setattr(reference, "MIN_REFERENCE_ACTIONS", N - 1)  # one constant moves both
    assert missing.missing(tasks, d, 1, reference=True) == [] and loader_accepts(d, "a")
    src = (SCRIPTS / "missing_honest.py").read_text()
    assert "from simrt.reference import" in src and "MIN_REFERENCE_ACTIONS =" not in src


DRIVER = """set -euo pipefail
source "{scripts}/box_env.sh"
box_init
fake_honest() {{  # $1 = task file, $2 = epochs: record the call, then "run" the next queued log into DIR
  printf '%s|%s\\n' "$(tr '\\n' ' ' < "$1")" "$2" >> "$CALLS"
  mkdir -p "$DIR"
  local next
  next=$(ls "$QUEUE" | head -n 1)
  if [ -n "$next" ]; then mv "$QUEUE/$next" "$DIR/"; fi
}}
honest_until_complete $FLAG "$TASKS" "$DIR" 1 fake_honest
"""


def run_stage(tmp_path: Path, name: str, flag: str, queue: list[list[EvalSample]],
              max_passes: int = 4) -> tuple[subprocess.CompletedProcess, list[str], Path]:
    """box_env.sh's honest_until_complete [FLAG] on tasks a and b; pass k "runs" queue[k] into DIR."""
    box, root = Box(tmp_path / name), tmp_path / name
    for k, samples in enumerate(queue, 1):
        write_log(root / f"queue/2026-11-20T0{k}-00-00_pass{k}.eval", samples)
    driver = touch(root / "driver.sh", DRIVER.format(scripts=SCRIPTS))
    calls, d = root / "calls.txt", root / "logs/honest"
    d.parent.mkdir()  # the stage scripts create logs/<milestone> before the honest stage
    extra = {"PYTHONPATH": os.environ["PYTHONPATH"]} if "PYTHONPATH" in os.environ else {}  # the simrt under test
    env = box.env(PYTHONDONTWRITEBYTECODE="1", CALLS=str(calls), QUEUE=str(root / "queue"), DIR=str(d),
                  TASKS=str(touch(root / "tasks.txt", "e a\ne b\n")), FLAG=flag, MAX_PASSES=str(max_passes), **extra)
    r = subprocess.run(["bash", str(driver)], env=env, cwd=root, capture_output=True, text=True, timeout=300)
    return r, calls.read_text().splitlines() if calls.exists() else [], d


def test_honest_until_complete_reruns_a_task_until_it_has_a_reference(tmp_path):
    first, rerun = [honest("a:p1", "a", N - 1), honest("b:p1", "b", N)], [honest("a:p2", "a", N)]
    r, calls, d = run_stage(tmp_path, "ref", "--reference", [first, rerun])
    assert r.returncode == 0, r.stderr
    assert [c.replace(" |", "|") for c in calls] == ["e a e b|1", "e a|1"]  # pass 2 reruns only a, 1 epoch
    assert "pass 2: 1 task(s) short of 1 clean run(s) or a run usable as a simulator reference" in r.stdout
    assert ("e a: no run usable as a simulator reference: "
            f"2026-11-20T01-00-00_pass1.eval a:p1:1: only {N - 1}") in r.stderr
    assert loader_accepts(d, "a") and loader_accepts(d, "b")
    tasks = touch(tmp_path / "t.txt", "e a\ne b\n")
    assert missing.selection(tasks, d, 1) == ({"e a": ["a:p1:1"], "e b": ["b:p1:1"]}, [])  # threshold unchanged
    # without --reference the stage stops after one pass and the loader refuses a, as before
    r, calls, d = run_stage(tmp_path, "plain", "", [first, rerun])
    assert r.returncode == 0 and len(calls) == 1 and not loader_accepts(d, "a"), r.stderr
    # a task that never gets a usable run fails the stage after MAX_PASSES, saying why
    r, calls, d = run_stage(tmp_path, "never", "--reference", [first, [honest("a:p2", "a", 1)]], max_passes=2)
    assert r.returncode != 0 and len(calls) == 2
    assert ("ERROR: 1 task(s) still lack 1 clean run(s) or a run usable as a simulator reference in "
            f"{d} after 2 passes") in r.stderr
