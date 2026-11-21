"""Infra tests: run manifests (simrt.manifest), the scripts' Python helpers (render_settings.py,
missing_honest.py, honest_pool.py) and static checks of setup_box.sh, .gitignore and pyproject.toml.
The box scripts themselves are tested in test_box_scripts.py. No model, network or Docker calls; git
runs only on temporary repos or read-only.
"""

import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from boxfakes import DUMMY_KEY, REPO, SCRIPTS, touch, write_eval

from simrt import manifest as mf
from simrt.monitoring import _key as monitor_key

# runpy compiles without writing scripts/__pycache__
render = SimpleNamespace(**runpy.run_path(str(SCRIPTS / "render_settings.py")))
missing = SimpleNamespace(**runpy.run_path(str(SCRIPTS / "missing_honest.py")))
pool = SimpleNamespace(**runpy.run_path(str(SCRIPTS / "honest_pool.py")))


# ---- simrt.manifest ------------------------------------------------------------------------------

def git(path: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)


def make_repo(path: Path, tag: str | None = None) -> Path:
    touch(path / "f.txt", "x")
    git(path, "init", "-q")
    git(path, "add", "f.txt")
    git(path, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "init")
    if tag:
        git(path, "tag", tag)
    return path


def fake_docker(cmd: list[str]) -> str | None:
    if cmd[0] != "docker":
        return mf.run_cmd(cmd)  # real (read-only) git on the temporary repos
    if cmd[1:3] == ["image", "ls"]:
        return '{"Repository":"linuxarena/env-web_scraping","Tag":"v1.0.1","Digest":"sha256:aa","ID":"sha256:11"}\nnot json'
    if cmd[1:3] == ["image", "inspect"]:
        return '["linuxarena/env-internet-simulator@sha256:bb"]'
    return "sha256:22"  # docker inspect internet-simulator


def test_manifest_records_code_images_and_configs_without_secrets(tmp_path):
    simrt_repo, ct = make_repo(tmp_path / "simrt"), make_repo(tmp_path / "ct")
    touch(ct / ".git/info/exclude", ".environments/\n")
    make_repo(ct / ".environments/web_scraping", tag="v1.0.1")
    (ct / ".environments/copied").mkdir()
    touch(simrt_repo / "f.txt", "edited")
    cfg = touch(tmp_path / "sim.json", '{"a": 1}')
    sim_image = "linuxarena/env-internet-simulator@sha256:bb"
    environ = {"ANTHROPIC_API_KEY": DUMMY_KEY, "ENV_IMAGE_TAG_WEB_SCRAPING": "v1.0.1", "HOME": "/x",
               "INTERNET_SIMULATOR_IMAGE": sim_image}
    m = mf.build_manifest([str(cfg)], {"threshold": "5.17"}, ["scripts/m3_run.sh", "optimize"], run=fake_docker,
                          repos={"simrt": simrt_repo, "control-tower": ct, "linuxarena-paper": None}, environ=environ)
    head = subprocess.run(["git", "-C", str(simrt_repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    s = m["repos"]["simrt"]
    assert s["commit"] == head.stdout.strip() and s["dirty"] is True and s["changed"] == ["f.txt"] and s["n_changed"] == 1
    assert m["repos"]["control-tower"]["dirty"] is False and m["repos"]["linuxarena-paper"] is None
    assert m["environments"]["web_scraping"]["tag"] == "v1.0.1" and m["environments"]["copied"] is None
    assert m["docker"]["images"] == [{"repository": "linuxarena/env-web_scraping", "tag": "v1.0.1",
                                      "digest": "sha256:aa", "id": "sha256:11"}]
    assert m["docker"]["internet_simulator_ref"] == sim_image
    assert m["docker"]["internet_simulator_repo_digests"] == [sim_image]
    assert m["docker"]["internet_simulator_container_image"] == "sha256:22"
    assert m["configs"] == [{"path": str(cfg.resolve()), "sha256": hashlib.sha256(b'{"a": 1}').hexdigest()}]
    assert m["versions"]["inspect_ai"] and m["params"] == {"threshold": "5.17"} and m["argv"][1] == "optimize"
    assert m["env"]["vars"] == {"ENV_IMAGE_TAG_WEB_SCRAPING": "v1.0.1", "INTERNET_SIMULATOR_IMAGE": sim_image}
    assert m["env"]["provider_keys_set"] == {"ANTHROPIC_API_KEY": True, "OPENAI_API_KEY": False,
                                             "OPENROUTER_API_KEY": False}
    assert DUMMY_KEY not in json.dumps(m) and m["created_utc"].endswith("+00:00") and m["inventory"] is None


def test_manifest_hashes_uncommitted_code(tmp_path):
    repo = make_repo(tmp_path / "simrt")
    assert mf.git_state(repo)["changes_sha256"] is None  # clean
    hashes = []
    for text in ("SYSTEM = 'edit A'\n", "SYSTEM = 'a different edit B'\n", "SYSTEM = 'edit A'\n"):
        touch(repo / "f.txt", text)
        hashes.append(mf.git_state(repo)["changes_sha256"])
    assert hashes[0] != hashes[1] and hashes[0] == hashes[2] and len(hashes[0]) == 64
    touch(repo / "new.py", "v1")  # untracked files count with their content
    with_new = mf.git_state(repo)["changes_sha256"]
    touch(repo / "new.py", "v2")
    assert len({hashes[0], with_new, mf.git_state(repo)["changes_sha256"]}) == 3
    (repo / "f.txt").write_bytes(b"caf\xe9 in latin-1\n")  # not UTF-8: no crash, still hashed
    assert mf.git_state(repo)["changes_sha256"] not in hashes


def test_manifest_inventory_lists_what_the_run_dir_already_held(tmp_path, monkeypatch):
    out = tmp_path / "run"
    touch(out / "rounds.jsonl", '{"round": 0}\n{"round": 1}\n{"rou')  # two complete records
    touch(out / "round_00/eval/a.eval", "x")
    touch(out / "rep0/manifest.json", "{}")  # a subdirectory with its own manifest is its own record
    touch(out / "rep0/b.eval", "y")
    touch(out / "manifest.json", "{}")
    inv = mf.inventory(out)
    assert set(inv["files"]) == {"rounds.jsonl", "round_00/eval/a.eval"} and inv["truncated"] is False
    assert inv["files"]["rounds.jsonl"]["lines"] == 2 and inv["files"]["round_00/eval/a.eval"]["bytes"] == 1
    assert "lines" not in inv["files"]["round_00/eval/a.eval"]
    monkeypatch.setattr(mf, "MAX_INVENTORY", 1)
    assert mf.inventory(out)["truncated"] is True and mf.inventory(tmp_path / "none")["files"] == {}


def test_manifest_finds_the_running_repos_and_skips_missing_docker_and_git_quietly(tmp_path):
    m = mf.build_manifest([], {}, ["x"], run=lambda cmd: None, repos={"simrt": tmp_path}, environ={})
    assert m["docker"] is None and m["repos"] == {"simrt": None} and m["environments"] == {}
    repos = mf.locate_repos()
    assert repos["simrt"] == REPO / "src" / "simrt" and repos["linuxarena-paper"] == Path(sys.prefix).resolve()
    assert repos["control-tower"] is not None and repos["control-tower"].name == "control_tower"


def test_manifest_cli_writes_latest_and_keeps_history(tmp_path, monkeypatch):
    monkeypatch.setattr(mf, "run_cmd", lambda cmd: None)  # no git or docker in this test
    monkeypatch.setattr(mf, "locate_repos", lambda: {})
    cfg, out = touch(tmp_path / "pairs.txt", "e m s\n"), tmp_path / "run"
    for t in ("5.0", "5.17"):
        mf.main(["--out", str(out), "--config", str(cfg), "--param", f"threshold={t}",
                 "--", "scripts/m3_run.sh", "optimize"])
        touch(out / "rounds.jsonl", '{"round": 0}\n')  # written by the stage after its manifest
    history = [json.loads(x) for x in (out / "manifests.jsonl").read_text().splitlines()]
    latest = json.loads((out / "manifest.json").read_text())
    assert [h["params"]["threshold"] for h in history] == ["5.0", "5.17"] and latest == history[-1]
    assert [h["inventory"]["files"].get("rounds.jsonl", {}).get("lines") for h in history] == [None, 1]
    assert latest["argv"] == ["scripts/m3_run.sh", "optimize"] and len(latest["configs"][0]["sha256"]) == 64
    with pytest.raises(SystemExit, match="config file not found"):
        mf.main(["--out", str(out), "--config", str(tmp_path / "nope.json")])
    with pytest.raises(SystemExit):
        mf.main(["--out", str(out), "--param", "novalue"])


# ---- scripts/render_settings.py ------------------------------------------------------------------

@pytest.mark.parametrize("name,bedrock_routes", [("sim_m1.json", 3), ("sim_haiku.json", 2)])
def test_committed_sim_configs_render_for_any_user(tmp_path, monkeypatch, name, bedrock_routes):
    monkeypatch.setenv("HOME", str(tmp_path))
    template = json.loads((REPO / "config" / name).read_text())
    for drop in (False, True):
        s = render.render(template, drop)
        assert set(s) == set(template)
        assert all(str(s[k]).startswith(str(tmp_path / "work")) for k in render.PATH_FIELDS)
        routes = str(s["model"]).removeprefix("rotate/").split(",")
        assert len(routes) == bedrock_routes + (0 if drop else 1)
        assert any(render.is_api_route(r) for r in routes) != drop


def test_render_checks_inputs_and_drops_only_api_routes(tmp_path):
    assert render.drop_api_routes("anthropic/bedrock/us.x@us-east-1") == "anthropic/bedrock/us.x@us-east-1"
    with pytest.raises(SystemExit, match="only direct-API routes"):
        render.drop_api_routes("rotate/anthropic/claude-haiku-4-5")
    fallback = render.render({"model": "anthropic/bedrock/a", "fallback_model": "rotate/anthropic/bedrock/b,"
                              "anthropic/claude-opus-4-6", "max_attempts": 3}, True)
    assert fallback == {"model": "anthropic/bedrock/a", "fallback_model": "rotate/anthropic/bedrock/b", "max_attempts": 3}
    s = {"model": "m", "envs_dir": str(tmp_path / "envs"), "reference_dir": str(tmp_path / "ref"), "trace_dir": "t"}
    with pytest.raises(SystemExit, match="setup_box"):
        render.check_inputs(s)
    (tmp_path / "envs").mkdir()
    with pytest.raises(SystemExit, match="m1_pipeline.sh honest"):
        render.check_inputs(s)
    touch(tmp_path / "ref" / "h.eval")
    render.check_inputs(s)


# ---- scripts/missing_honest.py and scripts/honest_pool.py ----------------------------------------

def test_missing_honest_counts_only_clean_honest_samples(tmp_path):
    d = tmp_path / "evals"
    write_eval(d / "a.eval", [("e", "a", None, False), ("e", "a", None, False), ("e", "b", None, False),
                              ("e", "b", None, True), ("e", "c", "side", False)])
    (d / "broken.eval").write_bytes(b"not a zip")  # an interrupted run's log is skipped, not fatal
    tasks = touch(tmp_path / "t.txt", "e a\ne b\ne c\ne d\n# e x\n")
    assert missing.missing(tasks, d, 2) == ["e b", "e c", "e d"]
    assert missing.missing(tasks, d, 1) == ["e c", "e d"]
    assert missing.missing(tasks, tmp_path / "none", 1) == ["e a", "e b", "e c", "e d"]


def test_missing_honest_selects_the_first_clean_runs_and_fails_when_short(tmp_path, capsys):
    d = tmp_path / "evals"  # run order is the log file name (ct starts it with the UTC start time)
    write_eval(d / "2026-11-20T01-00-00_pass1.eval", [("e", "a", None, None, 1), ("e", "a", None, "solver_error", 2),
                                                      ("e", "b", None, "error", 1)], tag="p1")
    write_eval(d / "2026-11-20T02-00-00_pass2.eval", [("e", "a", None, None, 1), ("e", "b", None, None, 1)], tag="p2")
    write_eval(d / "0old/2026-11-20T00-00-00_x.eval", [("e", "a", None, None, 1)], tag="x")  # not DIR/*.eval
    tasks = touch(tmp_path / "t.txt", "e a\ne b\n")
    chosen, short = missing.selection(tasks, d, 2)
    assert chosen["e a"] == [monitor_key("a:p1", 1), monitor_key("a:p2", 1)] == ["a:p1:1", "a:p2:1"]
    assert short == ["e b"] and missing.selection(tasks, d, 1) == ({"e a": ["a:p1:1"], "e b": ["b:p2:1"]}, [])
    out = tmp_path / "sel.json"
    with pytest.raises(SystemExit, match="1 task.*fewer than 2 clean.*e b"):
        missing.main(["--task-file", str(tasks), "--evals", str(d), "--epochs", "2", "--select", str(out)])
    assert not out.exists()
    missing.main(["--task-file", str(tasks), "--evals", str(d), "--epochs", "1", "--select", str(out)])
    assert json.loads(out.read_text())["samples"] == {"e a": ["a:p1:1"], "e b": ["b:p2:1"]}
    capsys.readouterr()
    missing.main(["--task-file", str(tasks), "--evals", str(d), "--epochs", "2"])
    assert capsys.readouterr().out == "e b\n"


def test_honest_pool_threshold_uses_only_selected_samples_and_guards_the_file(tmp_path, capsys):
    keys = ["a:1:1", "a:1:2", "b:2:1", "b:3:1"]
    sel = touch(tmp_path / "sel.json", json.dumps({"samples": {"e a": keys[:2], "e b": keys[2:]}}))
    scores = touch(tmp_path / "scores.json", json.dumps({**{k: {"sus": 1.0} for k in keys},
                                                         "b:0:1": {"sus": 9.0}}))  # the aborted run, not selected
    out, s, p = tmp_path / "threshold.txt", str(scores), str(sel)
    pool.main(["threshold", "--out", str(out), "--expect", "4", "--pool", s, p])
    assert out.read_text() == "1.0\n" and "honest n=4" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="expected 5 honest samples, got 4"):
        pool.main(["threshold", "--out", str(tmp_path / "t2.txt"), "--expect", "5", "--pool", s, p])
    lost = touch(tmp_path / "lost.json", json.dumps({"samples": {"e a": ["a:9:1"]}}))
    with pytest.raises(SystemExit, match="1 selected honest sample.*no monitor score.*a:9:1"):
        pool.main(["threshold", "--out", str(tmp_path / "t3.txt"), "--pool", s, str(lost)])
    with pytest.raises(SystemExit, match="takes SCORES SELECTED"):
        pool.main(["threshold", "--out", str(out), "--pool", s])
    # the committed rule counts every scored sample and only warns about the pool size
    legacy = ["threshold", "--legacy", "--expect", "4", "--pool", s]
    with pytest.raises(SystemExit, match=r"holds 1.0, but this run gives 9.0.*--force"):
        pool.main([*legacy, "--out", str(out)])
    assert "WARNING: expected 4 honest samples, got 5" in capsys.readouterr().out
    assert out.read_text() == "1.0\n" and (tmp_path / "threshold.new.txt").read_text() == "9.0\n"
    pool.main([*legacy, "--out", str(out), "--force"])
    assert out.read_text() == "9.0\n" and (tmp_path / "threshold.prev.txt").read_text() == "1.0\n"
    pool.main([*legacy, "--out", str(out)])
    assert "unchanged" in capsys.readouterr().out


def test_honest_pool_copies_only_the_selected_monitored_trajectories(tmp_path):
    sel = touch(tmp_path / "sel.json", json.dumps({"samples": {"e a": ["a:1:1"], "e b": ["b:2:1"]}}))
    src, dst = tmp_path / "mon", tmp_path / "pool"
    for name, sid in (("t1", "a:1"), ("t2", "b:2"), ("t0", "a:0")):  # a:0 is the aborted, rerun task
        touch(src / f"{name}.json", json.dumps({"eval_sample_id": sid, "epoch": 1, "traj_monitor_response": {}}))
    touch(dst / "stale.json", "{}")
    argv = ["trajs", "--selected", str(sel), "--src", str(src), "--dst", str(dst)]
    pool.main(argv)
    assert sorted(p.name for p in dst.iterdir()) == ["t1.json", "t2.json"]
    touch(src / "copy.json", (src / "t1.json").read_text())
    with pytest.raises(SystemExit, match="two monitored trajectories for a:1:1"):
        pool.main(argv)
    for p in (src / "copy.json", src / "t2.json"):
        p.unlink()
    with pytest.raises(SystemExit, match="1 selected honest sample.*no monitored trajectory.*b:2:1"):
        pool.main(argv)


# ---- setup_box.sh, .gitignore, pyproject.toml ----------------------------------------------------

def test_setup_box_pins_the_checked_out_revisions():
    text = (SCRIPTS / "setup_box.sh").read_text()
    pins = dict(re.findall(r"^(PAPER_REV|CT_REV)=([0-9a-f]{40})$", text, re.M))
    assert set(pins) == {"PAPER_REV", "CT_REV"} and "--depth 1" not in text
    assert 'clone_at linuxarena-paper https://github.com/redwoodresearch/linuxarena-paper.git "$PAPER_REV"' in text
    for key, sub in (("PAPER_REV", "linuxarena-paper"), ("CT_REV", "control-tower")):
        ext = REPO / "external" / sub
        if (ext / ".git").exists():
            head = subprocess.run(["git", "-C", str(ext), "rev-parse", "HEAD"], capture_output=True, text=True)
            assert pins[key] == head.stdout.strip()


def test_gitignore_covers_secrets_and_logs_and_test_extra_is_declared():
    for p in (".env.local", "prod.env", "credentials.json", "id_rsa", "id_ed25519.pub", "notes/run.eval", "x.key"):
        assert subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", p]).returncode == 0, p
    extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["optional-dependencies"]["test"]
    assert {re.split(r"[<>=]", e)[0] for e in extras} == {"pytest", "pytest-asyncio"}
