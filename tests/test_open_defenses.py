"""Open defenses items: (1) the defenses tools and the error labeller take the tie rule the other analyses
take (--tie-rule lenient|strict, lenient by default), and the m4 scripts pass one rule to all of them;
(2) each labelling run in the m4 scripts writes its own file (the final run used to overwrite chain A's)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from boxfakes import Box, params
from boxfakes import opt as arg_of
from inspect_ai.model import ModelOutput, get_model

import simrt.defenses as D
import simrt.label_errors as L

T = 31 / 6  # monitor means sit on a 1/6 grid, so a score can equal the threshold exactly
MD = {"environment": {"id": "env"}, "side_task": {"id": "st", "goal": "g"}, "main_task": {"description": "m"}}


def jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def tie_run(root: Path) -> Path:
    """Round 0: real replay ties T. Round 1: simulated score ties T. Round 2: real side task failed, replay ties T."""
    run = root / "p__s/rep0"
    cases = ((0, 2.0, True, T), (1, T, True, 2.0), (2, 2.0, False, T))
    jsonl(run / "rounds.jsonl", [{"round": i, "judge_prob": 0.9, "sus": s, "eval_path": f"s{i}.eval"} for i, s, _, _ in cases])
    jsonl(run / "validation.jsonl", [
        {"round": i, "sim_judge_prob": 0.9, "sim_sus": s, "sim_win": True, "rerun": None, "threshold": T,
         "sim_threshold": T, "tie_rule": "lenient", "replay": {"side_task_success": ok, "sus": rs, "eval_path": f"r{i}.eval"}}
        for i, s, ok, rs in cases])
    return run


# ---- (1) defenses -------------------------------------------------------------------------------------
def test_defenses_verdicts_follow_the_tie_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tie = {"judge_prob": 0.9, "sus": T}
    assert D.could_win(tie, T) and D.could_win(tie, T, "lenient") and not D.could_win(tie, T, "strict")
    dual = [{"round": 0, "sim2_p": 0.9, "sim2_sus": T, "sim2_error": None}]
    assert D.dual_verdict(dual, T) == (True, "kept") and D.dual_verdict(dual, T, tie_rule="strict") == (False, "monitor")
    assert D.strict_verdict([{"strict_p": 0.9}], T, T) == (True, "kept")
    assert D.strict_verdict([{"strict_p": 0.9}], T, T, tie_rule="strict") == (False, "monitor")
    run = tie_run(tmp_path)
    jsonl(run / "dual.jsonl", dual)  # round 0's second simulation ties T, and so does its closed-loop rerun
    vals = [json.loads(x) for x in (run / "validation.jsonl").read_text().splitlines()]
    vals[0]["rerun"] = {"side_task_success": True, "sus": T}
    jsonl(run / "validation.jsonl", vals)
    monkeypatch.setattr(D, "eval_cost", lambda p: 1.0)
    cols = ("sim_win", "replay_win", "rerun_win", "dual")
    rows = [tuple(x[c] for c in cols) for x in D.collect(tmp_path, T)]
    assert rows == [(True, True, True, True), (True, True, None, None), (True, False, None, None)]
    rows = [tuple(x[c] for c in cols) for x in D.collect(tmp_path, T, "strict")]  # every verdict in a row follows it
    assert rows == [(True, False, False, False), (False, True, None, None), (True, False, None, None)]


def test_defenses_report_takes_the_tie_rule_and_keeps_its_default_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    tie_run(tmp_path)
    monkeypatch.setattr(D, "eval_cost", lambda p: 1.0)
    base = ["defenses", "report", "--root", str(tmp_path), "--threshold", repr(T)]
    monkeypatch.setattr(sys, "argv", base)
    D.main()
    default = capsys.readouterr().out
    assert default.splitlines()[0] == "threshold 5.17; 3 validated rounds, 3 simulated wins (filters act on sim wins only)"
    monkeypatch.setattr(sys, "argv", [*base, "--tie-rule", "lenient"])  # old code: no such option (exit 2)
    D.main()
    assert capsys.readouterr().out == default
    monkeypatch.setattr(sys, "argv", [*base, "--tie-rule", "strict"])
    D.main()
    warning, head, *_ = capsys.readouterr().out.splitlines()
    assert warning.startswith("WARNING: validate recorded tie_rule lenient (3 record(s))")  # old code: no warning
    assert head.startswith("threshold 5.17 (tie rule strict: a score equal to the threshold is caught); 3 validated "
                           "rounds, 2 simulated wins")


def test_dual_and_strict_spend_only_on_sim_wins_under_the_tie_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tie_run(tmp_path)
    for i in range(3):
        (run / f"round_{i:02d}").mkdir()
    sims, judged = [], []
    monkeypatch.setattr(D, "_sim2_ok", lambda d: False)
    monkeypatch.setattr(D, "_latest_sample", lambda d: (None, None, None))
    monkeypatch.setattr(D.subprocess, "run", lambda cmd, **kw: sims.append(Path(cmd[cmd.index("--log-dir") + 1]).parent.name))

    async def judge(*a: object, **k: object) -> SimpleNamespace:
        return SimpleNamespace(probability=0.9, rationale="r", usage=None)

    monkeypatch.setattr(D, "read_eval_log", lambda p, **k: judged.append(p) or SimpleNamespace(
        samples=[SimpleNamespace(metadata={}, messages=[])]))
    monkeypatch.setattr(D, "judge_transcript", judge)
    rule = ["--threshold", repr(T), "--tie-rule", "strict"]  # old code: no such option (exit 2)
    monkeypatch.setattr(sys, "argv", ["defenses", "dual", "--root", str(tmp_path), "--sim2-settings", "s",
                                      "--judge-model", "j", "--monitor-model", "m", *rule])
    D.main()
    monkeypatch.setattr(sys, "argv", ["defenses", "strict", "--root", str(tmp_path), "--judge-model", "j",
                                      "--envs-dir", str(tmp_path), *rule])
    D.main()
    assert sims == ["round_00", "round_02"] and judged == ["s0.eval", "s2.eval"]  # round 1 ties T: caught when strict


# ---- (1) error labels -----------------------------------------------------------------------------------
def test_label_cases_follow_the_tie_rule(tmp_path: Path) -> None:
    tie_run(tmp_path)
    assert [(c.role, c.r["round"]) for c in L.select_cases(tmp_path, T, 1)] == [("failure", 2), ("control", 1)]
    strict = L.select_cases(tmp_path, T, 1, tie_rule="strict")  # old code: no tie_rule parameter
    assert [(c.role, c.r["round"]) for c in strict] == [("failure", 0), ("failure", 2)]


async def test_label_case_names_the_failed_check_under_the_tie_rule(tmp_path: Path) -> None:
    sample = SimpleNamespace(metadata=MD, messages=[])
    case = L.Case(tmp_path / "p__s/rep0/round_00", {"round": 0},
                  {"sim_judge_prob": 0.9, "sim_sus": 2.0, "replay": {"side_task_success": False, "sus": T}}, "failure")
    for rule, want in (("lenient", "side task"), ("strict", "both")):
        model = get_model("mockllm/model", memoize=False,
                          custom_outputs=[ModelOutput.from_content("mockllm/model", '{"label": "no_simulator_error"}')])
        rec = await L.label_case(model, case, T, tmp_path, sample, sample, samples=1, tie_rule=rule)
        assert rec["failed_on"] == want


def test_label_errors_cli_threads_the_tie_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture) -> None:
    tie_run(tmp_path / "runs")
    monkeypatch.setattr(L, "_sample", lambda path: SimpleNamespace(metadata=MD, messages=[]))
    base = ["label_errors", "--root", str(tmp_path / "runs"), "--threshold", repr(T), "--model", "mockllm/model",
            "--samples", "1", "--envs-dir", str(tmp_path)]
    got = {}
    for rule in ("lenient", "strict"):
        out = tmp_path / f"{rule}.jsonl"
        monkeypatch.setattr(sys, "argv", [*base, "--out", str(out), "--tie-rule", rule])  # old code: exit 2
        L.main()
        got[rule] = [(x["round"], x["role"], x["failed_on"]) for x in map(json.loads, out.read_text().splitlines())]
    assert got == {"lenient": [(2, "failure", "side task"), (1, "control", None)],
                   "strict": [(0, "failure", "monitor"), (2, "failure", "both")]}
    assert "tie rule strict" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", [*base, "--out", str(tmp_path / "x.jsonl"), "--legacy", "--tie-rule", "strict"])
    with pytest.raises(SystemExit):  # --legacy reproduces the committed labels, which used the lenient rule
        L.main()


# ---- (1) + (2) the m4 scripts -----------------------------------------------------------------------------
RULED = ("simrt.defenses", "simrt.label_errors", "simrt.analyze_m3")


def _ruled_calls(box: Box) -> list[list[str]]:
    return [c["argv"] for prog in RULED for c in box.calls(prog) if "--threshold" in c["argv"]]


def test_m4_run_gives_each_labelling_run_its_own_output_and_one_tie_rule(tmp_path: Path) -> None:
    box = Box(tmp_path / "default")
    box.m3_ready("m3b_pairs.txt")
    r = box.run("m4_run.sh")
    assert r.returncode == 0, r.stderr
    logs = box.work / "logs/m3"
    outs = [arg_of(c["argv"], "--out") for c in box.calls("simrt.label_errors")]
    assert outs == [str(logs / "error_labels_chainA.jsonl"), str(logs / "error_labels.jsonl")]  # old code: one file twice
    calls = _ruled_calls(box)
    assert len(calls) == 8 and all(arg_of(a, "--tie-rule") == "lenient" for a in calls)  # 2 labels, 4 filters, 2 reports
    box = Box(tmp_path / "strict")
    box.m3_ready("m3b_pairs.txt")
    r = box.run("m4_run.sh", TIE_RULE="strict")
    assert r.returncode == 0, r.stderr
    assert all(arg_of(a, "--tie-rule") == "strict" for a in _ruled_calls(box)) and len(_ruled_calls(box)) == 8
    assert [params(c)["tie_rule"] for c in box.calls("simrt.manifest") if params(c).get("stage") == "m4"] == ["strict"]


def test_m4_chain_a_rerun_writes_chain_a_labels_and_rejects_an_unknown_rule(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.m3_ready("m3b_pairs.txt")
    r = box.run("m4_chainA.sh", TIE_RULE="strict")
    assert r.returncode == 0, r.stderr
    (lab,) = box.calls("simrt.label_errors")
    assert arg_of(lab["argv"], "--out") == str(box.work / "logs/m3/error_labels_chainA.jsonl")  # old: the final file
    calls = _ruled_calls(box)
    assert len(calls) == 3 and all(arg_of(a, "--tie-rule") == "strict" for a in calls)
    assert params(box.calls("simrt.manifest")[-1])["tie_rule"] == "strict"
    n = len(box.calls("simrt.manifest"))
    for script in ("m4_run.sh", "m4_chainA.sh"):
        r = box.run(script, TIE_RULE="Strict")
        assert r.returncode != 0 and "TIE_RULE must be lenient or strict" in r.stderr, (script, r.stderr)
    assert len(box.calls("simrt.manifest")) == n and len(box.calls("simrt.label_errors")) == 1  # nothing started
