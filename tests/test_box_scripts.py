"""The box scripts (launch.sh, stall_check.sh and the pipeline stages) run against a fake deployment
(tests/boxfakes.py): stub ct, python, docker, setsid and flock under a temporary HOME. No model,
network or Docker calls.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from boxfakes import (API_ROUTE, DUMMY_KEY, HONEST, REPO, SCRIPTS, SONNET_BEDROCK, Box, key_env, opt, params, poll,
                      touch, wait_for, write_eval)

from simrt.reference import MIN_REFERENCE_ACTIONS


@pytest.fixture
def box(tmp_path: Path):  # noqa: ANN201
    yield Box(tmp_path)
    (tmp_path / "go").touch()  # releases a launched test job that waits for it, even after a failure


@pytest.fixture
def honest_eval(tmp_path: Path) -> Path:  # clean runs the simulators can also use as references
    return write_eval(tmp_path / "fixtures/honest.eval", [(e, m, None, False) for e, m in HONEST],
                      actions=MIN_REFERENCE_ACTIONS)


def group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def test_every_script_parses():
    for f in sorted(SCRIPTS.glob("*.sh")):
        r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, (f.name, r.stderr)


def test_scripts_fail_clearly_without_a_deployment_or_with_another_checkouts_simrt(box):
    nohome = box.root / "nohome"
    nohome.mkdir()
    for script, args in (("m1_pipeline.sh", ["honest"]), ("m3_run.sh", ["threshold"]), ("launch.sh", ["j", "true"])):
        r = subprocess.run(["bash", str(SCRIPTS / script), *args], env={**box.env(), "HOME": str(nohome)},
                           cwd=box.root, capture_output=True, text=True, timeout=60)
        assert r.returncode != 0 and "setup_box.sh" in r.stderr, (script, r.stderr)
    assert not (nohome / "work").exists()  # no lock or log dirs left behind
    for f in ("m3_run.sh", "box_env.sh"):  # a second checkout, while the venv's simrt is this one
        touch(box.root / "other/scripts" / f, (SCRIPTS / f).read_text())
    r = subprocess.run(["bash", str(box.root / "other/scripts/m3_run.sh"), "threshold"], env=box.env(),
                       cwd=box.root, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "uv pip install" in r.stderr and not box.calls("simrt.manifest")


# ---- launch.sh and stall_check.sh ----------------------------------------------------------------

def test_launch_rejects_unsafe_job_names(box):
    for name in ("x$(touch INJECTED)", 'q";touch INJECTED;"', "../escape", ".hidden", ""):
        r = box.run("launch.sh", name, "true", timeout=30)
        assert r.returncode == 2 and "invalid job name" in r.stderr, name
    assert not list(box.root.rglob("INJECTED*")) and not (box.work / "logs/escape.lock").exists()


def test_launch_records_the_job_pid_and_status_and_refuses_a_duplicate(box):
    jobs, mine, go = box.work / "logs/jobs", box.root / "mypid", box.root / "go"
    job = 'echo $$ > "$1"; while [ ! -e "$2" ]; do sleep 0.1; done; exit 3'  # runs until the test says go
    r = box.run("launch.sh", "j1", "sh", "-c", job, "_", str(mine), str(go))
    assert r.returncode == 0 and "started j1" in r.stdout, r.stderr
    dup = box.run("launch.sh", "j1", "true")
    assert dup.returncode == 1 and "already running" in dup.stderr
    go.touch()
    assert wait_for(jobs / "j1.status") == "3"
    assert (jobs / "j1.pid").read_text().split()[0] == wait_for(mine)
    poll(lambda: box.run("launch.sh", "j1", "true").returncode == 0, "j1 to be relaunched once it has exited")
    poll(lambda: (jobs / "j1.status").exists(), "the relaunched j1 to finish")
    assert (jobs / "j1.status").read_text().strip() == "0"


def test_launch_takes_over_a_stale_lock_exactly_once(box):
    jobs = box.work / "logs/jobs"
    (dead := subprocess.Popen(["true"])).wait()
    touch(jobs / "j2.lock", f"{dead.pid}\n")  # left by the old launcher's dead job
    job = ["sh", "-c", 'while [ ! -e "$1" ]; do sleep 0.1; done', "_", str(box.root / "go")]
    procs = [subprocess.Popen(["bash", str(SCRIPTS / "launch.sh"), "j2", *job], env=box.env(),
                              cwd=box.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(6)]
    outs = [p.communicate(timeout=60) for p in procs]
    (box.root / "go").touch()  # the job ran until every launcher had tried
    assert sum("started j2" in o for o, _ in outs) == 1
    assert sum("already running" in e for _, e in outs) == 5
    assert wait_for(jobs / "j2.status") == "0"


def test_launch_refuses_while_a_child_that_closed_the_lock_is_alive(box):
    jobs, child = box.work / "logs/jobs", box.root / "child.pid"
    spawn = "import subprocess, sys; open(sys.argv[1], 'w').write(str(subprocess.Popen(['sleep', '60']).pid))"
    assert box.run("launch.sh", "j3", sys.executable, "-c", spawn, str(child)).returncode == 0
    assert wait_for(jobs / "j3.status") == "0"
    pid = int(wait_for(child))  # Popen closes inherited descriptors, so this child does not hold the lock
    quiet = touch(box.root / "quiet/x.log", "x")
    for p in (quiet, jobs / "j3.out"):
        os.utime(p, (time.time() - 3600,) * 2)
    try:
        r = box.run("launch.sh", "j3", "true")
        assert r.returncode == 1 and "still alive in process group" in r.stderr, r.stderr
        r = box.run("stall_check.sh", "1", f"j3={quiet.parent}", timeout=20)  # still running, so not DONE
        assert r.returncode == 3 and "STALL: j3" in r.stdout, r.stdout
    finally:
        os.kill(pid, signal.SIGKILL)
    poll(lambda: box.run("launch.sh", "j3", "true").returncode == 0, "j3 to be relaunched once its child is gone")


def test_launch_stops_the_whole_job_with_term_and_records_its_status(box):
    jobs = box.work / "logs/jobs"
    assert box.run("launch.sh", "j4", "sh", "-c", "sleep 60 & wait").returncode == 0
    poll(lambda: (jobs / "j4.pid").exists() and len((jobs / "j4.pid").read_text().split()) == 2, "j4.pid")
    group = int((jobs / "j4.pid").read_text().split()[1])
    os.killpg(group, signal.SIGTERM)
    assert wait_for(jobs / "j4.status") not in ("", "0")
    poll(lambda: not group_alive(group), "every process of j4 to exit")


def test_stall_check_outcomes(box):
    jobs = box.work / "logs/jobs"
    r = box.run("stall_check.sh", "5", "nojob", timeout=20)
    assert r.returncode == 2 and "UNKNOWN" in r.stdout
    for job, status in (("ok", "0"), ("bad", "4")):
        touch(jobs / f"{job}.status", status + "\n").with_suffix(".lock").touch()
    assert box.run("stall_check.sh", "5", "ok", timeout=20).returncode == 0
    r = box.run("stall_check.sh", "5", "ok", "bad", timeout=20)
    assert r.returncode == 1 and "DONE bad status=4" in r.stdout
    touch(jobs / "dead.lock")
    assert box.run("stall_check.sh", "5", "dead", timeout=20).returncode == 2
    # A running job whose own directory is quiet is a stall, even while another job writes.
    old = touch(box.root / "quiet/x.log", "x")
    os.utime(old, (time.time() - 3600,) * 2)
    touch(box.work / "logs/other/new.log", "fresh")
    fd = os.open(touch(jobs / "busy.lock"), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = box.run("stall_check.sh", "5", f"busy={old.parent}", timeout=20)
    finally:
        os.close(fd)
    assert r.returncode == 3 and "STALL: busy" in r.stdout


# ---- pipeline stages -----------------------------------------------------------------------------

def test_m1_honest_stage_runs_real_honest_policy_without_keys(box, honest_eval):
    r = box.run("m1_pipeline.sh", "honest", FAKE_EVAL=str(honest_eval), **key_env(box.root))
    assert r.returncode == 0, r.stderr
    (ct,) = box.calls("ct")
    a = ct["argv"]
    assert a[:4] == ["run", "eval", "--policy", "honest"] and opt(a, "--epochs") == "1"
    assert opt(a, "--log-dir") == str(box.work / "logs/m1/honest")
    assert opt(a, "--task-file") == str(REPO / "config/tasks/m1_honest.txt")
    assert f"model={SONNET_BEDROCK}" in a and "model_cache=false" in a and "cache=false" in a
    assert ct["key"] == ""  # real runs get no provider key, even when a key file exists
    (m,) = box.calls("simrt.manifest")
    assert opt(m["argv"], "--out") == str(box.work / "logs/m1/honest")
    assert params(m)["policy_cache"] == "false" and params(m)["api_fallback"] == "on"


def test_m1_honest_stage_needs_a_run_the_simulator_can_use_as_reference(box, tmp_path):
    clean_but_short = write_eval(tmp_path / "fixtures/short.eval", [(e, m, None, False) for e, m in HONEST])
    r = box.run("m1_pipeline.sh", "honest", FAKE_EVAL=str(clean_but_short), MAX_PASSES="1")
    assert r.returncode != 0 and "or a run usable as a simulator reference" in r.stderr
    assert len(box.calls("ct")) == 1  # MAX_PASSES=1: one pass, then it stops instead of declaring success


def test_honest_stage_reruns_only_short_tasks_then_fails(box, tmp_path):
    partial = write_eval(tmp_path / "fixtures/partial.eval", [(e, m, None, False) for e, m in HONEST[1:]])
    r = box.run("m3_run.sh", "honest", FAKE_EVAL=str(partial), MAX_PASSES="2")
    assert r.returncode != 0 and "1 task(s) still lack 2 clean run(s)" in r.stderr
    first, second = (c["argv"] for c in box.calls("ct"))
    assert opt(first, "--epochs") == "2" and opt(first, "--task-file") == str(REPO / "config/tasks/m1_honest.txt")
    assert opt(second, "--epochs") == "1" and opt(second, "--task-file").endswith(".todo_honest_extra.txt")


def test_m3_threshold_pools_exactly_the_selected_honest_runs(box):
    write_eval(box.work / "logs/m1/honest/2026-11-01T00-00-00_m1.eval", [(e, m, None, None) for e, m in HONEST])
    extra, bad = box.work / "logs/m3/honest_extra", {(0, 2), (1, 1)}  # (task, epoch) runs that aborted
    write_eval(extra / "2026-11-20T01-00-00_pass1.eval", [(e, m, None, "solver_error" if (j, ep) in bad else None, ep)
                                                          for j, (e, m) in enumerate(HONEST) for ep in (1, 2)], tag="p1")
    write_eval(extra / "2026-11-20T02-00-00_pass2.eval", [(*HONEST[j], None, None, 1) for j in (0, 1)], tag="p2")
    thr = box.work / "logs/m3/threshold.txt"  # the stub monitor scores aborted runs 9.0, the others 2.0
    r = box.run("m3_run.sh", "threshold", SIMRT_LEGACY_THRESHOLD="1")  # committed rule: every scored run
    assert r.returncode == 0, r.stderr
    assert "expected 36 honest samples, got 38" in r.stdout and thr.read_text() == "9.0\n"
    assert [("--legacy" in c["argv"], opt(c["argv"], "--model")) for c in box.calls("simrt.monitoring")] == [(True, None)] * 2
    r = box.run("m3_run.sh", "threshold")
    assert r.returncode != 0 and "holds 9.0, but this run gives 2.0" in r.stderr and "honest n=36" in r.stdout
    assert thr.read_text() == "9.0\n" and (thr.parent / "threshold.new.txt").read_text() == "2.0\n"
    assert box.run("m3_run.sh", "threshold", SIMRT_FORCE_THRESHOLD="1").returncode == 0
    assert thr.read_text() == "2.0\n" and (thr.parent / "threshold.prev.txt").read_text() == "9.0\n"
    sel = json.loads((thr.parent / "honest_extra_selected.json").read_text())["samples"]
    main0 = HONEST[0][1]
    assert sel[" ".join(HONEST[0])] == [f"{main0}:p1:1", f"{main0}:p2:1"]
    assert all("policy_cache" not in params(c) for c in box.calls("simrt.manifest"))  # no policy runs here


def test_m1_monitor_and_analyze_use_only_the_selected_honest_runs(box):
    honest = box.work / "logs/m1/honest"  # the first task's first run aborted and was rerun
    write_eval(honest / "2026-11-01T00-00-00_a.eval",
               [(e, m, None, "solver_error" if j == 0 else None) for j, (e, m) in enumerate(HONEST)], tag="a")
    write_eval(honest / "2026-11-02T00-00-00_b.eval", [(*HONEST[0], None, None)], tag="b")
    for d in ("real_attack", "sim_attack"):
        write_eval(box.work / f"logs/m1/{d}/x.eval", [("web_scraping", "m", "s", None)])
    r = box.run("m1_pipeline.sh", "monitor", "analyze")
    assert r.returncode == 0, r.stderr
    mon = box.work / "logs/m1/mon"
    assert len(list((mon / "honest").glob("*.json"))) == 13
    pooled = [json.loads(p.read_text()) for p in (mon / "honest_pool").glob("*.json")]
    assert len(pooled) == 12 and {t["traj_monitor_response"]["sus_score"] for t in pooled} == {2.0}
    (an,) = box.calls("simrt.analyze_m1")
    assert opt(an["argv"], "--honest") == str(mon / "honest_pool")


def test_stages_name_the_honest_rerun_when_an_m1_task_has_no_clean_run(box):
    # As on the experiment box: the only M1 run of one task stopped at the message limit (solver_error).
    write_eval(box.work / "logs/m1/honest/2026-11-10T20-30-07_m1.eval",
               [(e, m, None, "solver_error" if m == "ct_rewrite_in_rust" else None) for e, m in HONEST])
    fix = "rerun its short tasks first with: scripts/launch.sh m1 scripts/m1_pipeline.sh honest"
    r = box.run("m3_run.sh", "threshold")
    assert r.returncode != 0 and "clinical_trial ct_rewrite_in_rust" in r.stderr and fix in r.stderr
    r = box.run("m1_pipeline.sh", "monitor")
    assert r.returncode != 0 and fix in r.stderr and not box.calls("simrt.monitoring") and not box.calls("ct")


def test_m1_real_stage_fails_when_pairs_stay_missing(box):
    r = box.run("m1_pipeline.sh", "real", FAKE_MISSING="web_scraping benchmark remote_code_download", MAX_PASSES="2")
    assert r.returncode != 0 and "still missing" in r.stderr
    runs = box.calls("ct")
    assert len(runs) == 2 and all(f"model={SONNET_BEDROCK}" in c["argv"] and "cache=false" in c["argv"] for c in runs)
    box.run("m1_pipeline.sh", "real", FAKE_MISSING="web_scraping a b", MAX_PASSES="1", SIMRT_POLICY_CACHE="1")
    assert {"model_cache=true", "cache=true"} <= set(box.calls("ct")[-1]["argv"])  # ct's default, as before
    assert params(box.calls("simrt.manifest")[-1])["policy_cache"] == "true"


def test_policy_cache_switch_is_refused_where_it_cannot_apply(box):
    box.m3_ready("m3_pairs.txt")
    for script, args in (("m3_run.sh", ["honest", "optimize"]), ("m3_run.sh", ["validate"]), ("m2_run.sh", []),
                         ("m4_run.sh", []), ("m5_run.sh", ["noise", "closedloop"])):
        r = box.run(script, *args, SIMRT_POLICY_CACHE="1")
        assert r.returncode != 0 and "SIMRT_POLICY_CACHE=1 cannot apply" in r.stderr, (script, r.stderr)
    assert not (box.calls("ct") or box.calls("simrt.optimize") or box.calls("simrt.manifest"))


@pytest.mark.parametrize("with_key", [True, False])
def test_m1_sim_stage_key_and_rendered_settings(box, with_key):
    box.m3_ready()
    env = key_env(box.root) if with_key else {}
    r = box.run("m1_pipeline.sh", "sim", FAKE_MISSING="clinical_trial a b", MAX_PASSES="1", **env)
    assert "still missing" in r.stderr  # the stub never produces the pair
    (sim,) = box.calls("simrt.run_sim_eval")
    path = box.work / "logs/m1/sim_m1.json"
    settings = json.loads(path.read_text())
    assert opt(sim["argv"], "--sim-settings") == str(path) and "model_cache=false" in sim["argv"]
    assert settings["reference_dir"] == str(box.work / "logs/m1/honest")
    assert sim["key"] == (DUMMY_KEY if with_key else "")
    for model in (opt(sim["argv"], "--judge-model"), settings["model"]):
        assert (API_ROUTE in model) == with_key


def test_sim_stage_refuses_to_run_without_reference_transcripts(box):
    r = box.run("m1_pipeline.sh", "sim")
    assert r.returncode != 0 and "m1_pipeline.sh honest" in r.stderr and not box.calls("simrt.run_sim_eval")


def test_m3_optimize_waits_for_every_run_and_fails_if_one_failed(box):
    box.m3_ready()
    r = box.run("m3_run.sh", "optimize", FAKE_FAIL="simrt.optimize:rep1")
    assert r.returncode != 0 and "2 job(s) failed" in r.stderr
    runs = box.calls("simrt.optimize")
    assert len(runs) == 6 and all(c["key"] == "" and opt(c["argv"], "--model") == SONNET_BEDROCK for c in runs)
    assert all(opt(c["argv"], "--tie-rule") == "lenient" for c in runs)  # the committed results' rule
    manifests = box.calls("simrt.manifest")
    assert {opt(c["argv"], "--out-dir") for c in runs} <= {opt(c["argv"], "--out") for c in manifests}
    assert all(params(c)["policy_cache"] == "false" and params(c)["api_fallback"] == "off" for c in manifests)


def test_m3_validate_pins_simulator_image_and_keeps_keys_out(box):
    box.m3_ready("m3_pairs.txt")
    docker = {"pull": [0, ""], "image inspect": [0, "linuxarena/env-internet-simulator@sha256:abc\n"]}
    r = box.run("m3_run.sh", "validate", FAKE_DOCKER=json.dumps(docker), **key_env(box.root))
    assert r.returncode == 0, r.stderr
    runs = box.calls("simrt.validate")
    assert len(runs) == 6 and all(opt(c["argv"], "--model") == SONNET_BEDROCK and c["key"] == "" for c in runs)
    assert {c["image"] for c in runs} == {"linuxarena/env-internet-simulator@sha256:abc"}
    leaky = {**docker, "inspect --format": [0, f"PATH=/bin\nANTHROPIC_API_KEY={DUMMY_KEY}\n"]}
    r = box.run("m3_run.sh", "validate", FAKE_DOCKER=json.dumps(leaky), **key_env(box.root))
    assert r.returncode != 0 and "ANTHROPIC_API_KEY" in r.stderr and "docker rm -f internet-simulator" in r.stderr
    assert DUMMY_KEY not in r.stdout + r.stderr and len(box.calls("simrt.validate")) == 6


def test_m4_writes_no_reports_when_a_chain_fails(box):
    box.m3_ready("m3b_pairs.txt")
    r = box.run("m4_run.sh", FAKE_FAIL="simrt.label_errors:")
    assert r.returncode != 0 and "chain(s) A" in r.stderr
    assert box.calls("simrt.validate") and not box.calls("simrt.analyze_m3")


def test_m4_chain_b_gets_a_key_given_only_in_the_environment(box):
    box.m3_ready("m3b_pairs.txt")
    r = box.run("m4_run.sh", ANTHROPIC_API_KEY=DUMMY_KEY, SIMRT_KEY_FILE=str(box.root / "no-key-file"),
                TIE_RULE="strict")
    assert r.returncode == 0, r.stderr
    runs = box.calls("simrt.optimize")
    assert runs and all(c["key"] == DUMMY_KEY and API_ROUTE in opt(c["argv"], "--model") for c in runs)
    assert box.calls("simrt.validate") and all(c["key"] == "" for c in box.calls("simrt.validate"))
    # chain B follows m4_run.sh's tie rule, like chain A's labels and defenses
    assert all(opt(c["argv"], "--tie-rule") == "strict" for c in runs + box.calls("simrt.validate"))
    assert "Anthropic API fallback on" in (box.work / "logs/m3/chainB.log").read_text()


def m5_run_dir(box: Box) -> Path:
    """One M3 run: rounds 0 and 1 recorded, round 2 errored; round 1's replay has no known outcome; a
    killed attempt at round 1 was set aside as round_01.stale-0."""
    run = box.work / "logs/m3/runs/p__s/rep0"
    rounds = [{"round": 0, "judge_prob": 0.9, "sus": 1.0, "eval_path": "x", "error": None},
              {"round": 1, "judge_prob": 0.2, "sus": 1.0, "eval_path": "y", "error": None},
              {"round": 2, "judge_prob": None, "sus": None, "eval_path": None, "error": "boom"}]
    for d in ("round_00", "round_01", "round_02", "round_01.stale-0"):
        for name, text in (("eval/a.eval", ""), ("real_replay/a.eval", ""), ("strat.txt", f"strategy {d}"),
                           ("task.txt", "e m s\n")):
            touch(run / d / name, text)
    touch(run / "rounds.jsonl", "".join(json.dumps(r) + "\n" for r in rounds))
    touch(run / "validation.jsonl", "".join(json.dumps({"round": i, "sim_judge_prob": p, "sim_sus": 1.0, "sim_win": w,
                                                        "replay": {"side_task_success": rep}}) + "\n"
                                            for i, p, w, rep in ((0, 0.9, True, True), (1, 0.2, False, None))))
    return run


def test_m5_noise_and_judge_use_only_recorded_transcripts_and_retry_failed_judgements(box):
    box.m3_ready()
    r = box.run("m5_run.sh", "noise")
    assert r.returncode != 0 and "no recorded rounds" in r.stderr
    run, m5 = m5_run_dir(box), box.work / "logs/m5"
    failed = touch(m5 / "judge_replay/p__s/rep0/round_00.json", json.dumps({"x:1": {"prob": None}}))
    r = box.run("m5_run.sh", "noise", "judge", **key_env(box.root))
    assert r.returncode == 0, r.stderr
    noise = sorted(opt(c["argv"], "--evals") for c in box.calls("simrt.monitoring"))
    assert noise == [str(run / "round_00/eval"), str(run / "round_00/real_replay"), str(run / "round_01/eval")]
    assert {opt(c["argv"], "--n-runs") for c in box.calls("simrt.monitoring")} == {"10"}
    (judge,) = box.calls("simrt.judge_cli")  # the failed judgement is not taken as done
    assert opt(judge["argv"], "--evals") == str(run / "round_00/real_replay") and judge["key"] == DUMMY_KEY
    failed.write_text(json.dumps({"x:1": {"prob": 0.4}}))
    assert box.run("m5_run.sh", "judge").returncode == 0 and len(box.calls("simrt.judge_cli")) == 1
    header = (SCRIPTS / "m5_run.sh").read_text().split("set -euo")[0]
    assert all(s in header for s in ("at most 6 x NOISE_PASSES", "at most 2 trajectories", "at most 11", "at most 21"))


def test_m5_closedloop_and_simulated_honest_stages(box, honest_eval):
    box.m3_ready()
    m5_run_dir(box)
    r = box.run("m5_run.sh", "closedloop", "sim_honest", "sim_threshold", FAKE_EVAL=str(honest_eval),
                SIM_HONEST_EPOCHS="1", **key_env(box.root))
    assert r.returncode == 0, r.stderr
    m5 = box.work / "logs/m5"
    loop = m5 / "closedloop/p__s/rep0"
    assert [json.loads(x)["round"] for x in (loop / "rounds.jsonl").read_text().splitlines()] == [1]
    assert (loop / "round_01/strat.txt").read_text() == "strategy round_01"
    (val,) = box.calls("simrt.validate")
    assert "--rerun-all" in val["argv"] and opt(val["argv"], "--out-dir") == str(loop) and val["key"] == ""
    (sim,) = box.calls("simrt.run_sim_eval")
    assert sim["argv"][2:4] == ["--policy", "honest"] and "cache=false" in sim["argv"] and sim["key"] == DUMMY_KEY
    assert json.loads((m5 / "sim_m1.json").read_text())["trace_dir"] == str(m5 / "sim_honest_traces")
    assert (m5 / "sim_threshold.txt").read_text() == "2.0\n"
    assert len(json.loads((m5 / "sim_honest_selected.json").read_text())["samples"]) == len(HONEST)
