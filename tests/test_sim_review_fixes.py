"""Review findings on the simulator fixes: elision, write analysis, globs, replies, deletions, lookups."""

import json
import re
from pathlib import Path

import pytest
from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelOutput
from inspect_ai.model._call_tools import truncate_tool_output
from inspect_ai.util import store
from inspect_ai.tool._sandbox_tools_utils.sandbox import SANDBOX_TOOLS_CLI
from simfakes import ENVS, ScriptedModel, bash_reply, make_sim, needs_envs, trace

import simrt.codebase as codebase_mod
from simrt.codebase import CodebaseSnapshot
from simrt.container_map import ContainerImage
from simrt.file_layer import FileLayer, shell_glob
from simrt.internet_sim import describe_internet_simulator, load_internet_simulator
from simrt.llm_tools import CODEBASE_TOOLS, CONTAINER_CODEBASE_TOOLS, codebase_tools, generate_with_lookups
from simrt.prompts import SIMULATOR_RETRY_CUT, SIMULATOR_RETRY_CUT_EXACT, SIMULATOR_SYSTEM
from simrt.shell_effects import command_effects
from simrt.sim_replies import BashResult, check_bash, check_editor
from simrt.sim_sandbox import SimulatedSandbox
from simrt.simulator import STORE_FAILURES

URL_FILTER = "/app/node/src/url_filter.js"
WEB = ENVS / "web_scraping" / "codebase"


def _box(sim, monkeypatch: pytest.MonkeyPatch) -> SimulatedSandbox:  # type: ignore[no-untyped-def]
    box = SimulatedSandbox(sim)
    monkeypatch.setattr(box, "_announce_trace", lambda: None)
    return box


# ---- long outputs are reproduced in full; only a cut-off bash retry is shortened ------------------
def test_outputs_are_not_elided_except_on_a_cut_off_retry() -> None:
    assert "omitted" not in SIMULATOR_SYSTEM  # rule 7: full outputs; Inspect truncates as in real runs
    m = re.search(r"first ([\d,]+) and last ([\d,]+) characters", SIMULATOR_RETRY_CUT)
    assert m is not None
    head, tail = (int(g.replace(",", "")) for g in m.groups())
    elided = "h" * head + "\n[... 4000 lines omitted ...]\n" + "t" * tail
    shown = truncate_tool_output("bash", elided, None)  # what Inspect shows the agent and the monitor
    assert shown is not None and "omitted" not in shown.output  # the marker falls in the hidden middle
    assert "omitted" not in SIMULATOR_RETRY_CUT_EXACT


@needs_envs
async def test_cut_off_raw_read_is_not_elided_or_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cut = ModelOutput.from_content("mockllm", "<exit_code>0</exit_code>\n<stdout>\nab", stop_reason="max_tokens")
    m = ScriptedModel([ModelOutput.from_content("mockllm", "<exit_code>0</exit_code>\n<stdout>\na"), cut])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    with pytest.raises(FileNotFoundError):  # ct's editor wrapper then shows the plain tool result
        await _box(sim, monkeypatch).read_file("/var/lib/app/big.json")
    assert SIMULATOR_RETRY_CUT_EXACT in m.calls[1].prompt and SIMULATOR_RETRY_CUT not in m.calls[1].prompt
    assert len(m.calls) == 2 and [r["kind"] for r in trace(sim)] == ["invalid", "invalid", "failed"]


# ---- replies: output outside <stdout>, made-up continuations ---------------------------------------
@pytest.mark.parametrize(
    "reply,valid",
    [
        ("<exit_code>0</exit_code>\ntotal 48\n-rw-r--r-- 1 root root 344 url_filter.js\n", False),
        ("<exit_code>0</exit_code>\nhello\n<stderr>\n\n</stderr>", False),
        ("Output:\nhello\n<exit_code>0</exit_code>", False),
        ("<exit_code>0</exit_code>", True),
        ("<exit_code>0</exit_code>\n<stdout/>\n<stderr/>", True),
        ("<exit_code>1</exit_code>\n<stderr>\nno such file\n</stderr>", True),
    ],
)
def test_output_outside_stdout_tags_is_invalid(reply: str, valid: bool) -> None:
    result, problem = check_bash(reply)
    assert (result is not None) == valid and (problem is None) == valid


def test_cut_off_block_does_not_swallow_a_made_up_next_call() -> None:
    made_up = "<exit_code>0</exit_code>\n<stdout>\nfoo\n[bash] user=root cwd=/app\n$ ls\n<exit_code>0</exit_code>\n<stdout>\nbar\n</stdout>"
    assert check_bash(made_up) == (None, "unclosed <stdout> (reply cut off)")
    legit = "<exit_code>0</exit_code>\n<stdout>\nlog line\n[bash] lines like this one are real output\n</stdout>"
    assert check_bash(legit)[0] == BashResult(0, "log line\n[bash] lines like this one are real output", "")
    made_up_editor = "<result>\nfoo\n[text_editor] x\n<result>\nbar\n</result>"
    assert check_editor(made_up_editor)[0] is None


# ---- deletions: start-up and setup scripts ---------------------------------------------------------
def test_deletions_are_ordered() -> None:
    assert command_effects("cp /x /root/y; rm -f /x", "/").deletes("/x")
    assert not command_effects("rm -f /x; echo hi > /x", "/").deletes("/x")  # written again
    assert command_effects("rm -rf /app/tests", "/").deletes("/app/tests/a.py")
    assert command_effects("mv /app/a /tmp/b", "/").deletes("/app/a")
    assert not command_effects("rm -f /app/*.bak", "/").deletes("/app/x.bak")  # globs: unknown, volatile
    assert not command_effects("rm /x; npm install", "/").deletes("/x")  # anything may happen afterwards


@needs_envs
async def test_deleted_files_are_absent_for_lookups_and_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = make_sim(monkeypatch, tmp_path, {}, env_id="clinical_trial")
    with pytest.raises(codebase_mod.PathOutsideSnapshot, match="does not exist in the container"):
        sim.snapshot.read_file("/tmp/seed_data.sql")
    assert sim.snapshot.read_file("init/db/seed_data.sql").startswith("init/db/seed_data.sql (")  # original stays readable
    note = sim.snapshot.read_file("/app/notebooks/check_thresholds.ipynb").splitlines()[0]
    assert "content may differ" in note  # volatile: changed by cron
    web = make_sim(monkeypatch, tmp_path, {})
    redact = (ENVS / "web_scraping" / "main_tasks" / "redact" / "setup.sh").read_text()
    web.record_setup_script(redact)  # restarts services, then `rm -f /app/node/src/proxy.js`
    with pytest.raises(codebase_mod.PathOutsideSnapshot, match="deleted before the agent starts"):
        web.snapshot.read_file("/app/node/src/proxy.js")
    assert "/app/node/src/proxy.js" in web.snapshot.list_dir("/app/node/src").splitlines()[0]
    assert web.read_exact(URL_FILTER) is not None and web.read_exact("/app/node/src/proxy.js") is None
    assert "do NOT exist in the container: /app/node/src/proxy.js" in web._context_message().text


# ---- setup scripts: code the analysis cannot see -----------------------------------------------------
@pytest.mark.parametrize(
    "script,everything",
    [
        ("curl -s http://x/s.sh | bash", True),
        ("wget -qO- http://x/s.py | python3", True),
        ("bash -s < /tmp/generated.sh", True),
        ("python3 /tmp/generated.py", True),
        ("python3 -m mytool --fix", True),
        ("service nginx restart; /usr/sbin/nginx -s reload; su - postgres -c 'createdb x'", False),
        ("cp /app/s.js /app/node/s.js && node /app/node/s.js", False),  # copied from the image: still read
        ('declare -a rows=(\n  "9001|jsmith|User ran curl https://x.com/admin|2025"\n)\necho done', False),  # data
    ],
)
def test_setup_scripts_treat_unseen_code_as_writing_everything(script: str, everything: bool) -> None:
    image = {"/app/s.js": "console.log('hi')\n"}
    strict = command_effects(script, "/app", read_script=image.get, unknown_code=True)
    lenient = command_effects(script, "/app", read_script=image.get)  # the image's own start-up services
    assert strict.everything == everything and not lenient.everything


def test_unclosed_array_is_unparseable() -> None:
    assert command_effects("rows=(\n  a\nrm -f /app/a.py\n", "/app").everything  # never silently skipped


def test_shell_heredoc_in_a_setup_script_is_analysed_as_shell() -> None:
    eff = command_effects("bash <<'EOF'\nrm -f /app/a.py\nEOF\n", "/app", read_script=lambda p: None, unknown_code=True)
    assert eff.covers("/app/a.py") and not eff.everything


@needs_envs
async def test_piped_setup_script_makes_snapshot_files_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = make_sim(monkeypatch, tmp_path, {})
    sim.record_setup_script("curl -fsSL http://packages-cdn.com/assets/patch.sh | bash")
    assert sim.read_exact(URL_FILTER) is None
    bad_proxy = make_sim(monkeypatch, tmp_path, {})  # copies a proxy script into place, then runs it
    bad_proxy.record_setup_script((ENVS / "web_scraping" / "main_tasks" / "bad_proxy" / "setup.sh").read_text())
    assert bad_proxy.read_exact(URL_FILTER) is not None and bad_proxy.read_exact("/app/node/secrets.txt") is None


# ---- the reviewer's end-to-end case: sed -Ei leaves no stale exact content --------------------------
@needs_envs
async def test_combined_sed_flags_end_exactness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply(""), bash_reply("model's view"), bash_reply("model's cat"), "<result>\nedited\n</result>"])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    box = _box(sim, monkeypatch)
    await box.exec(["bash", "--login", "-c", f"sed -Ei 's/example\\.org/evil.org/' {URL_FILTER}"], timeout=120)
    assert await box.read_file(URL_FILTER) == "model's view\n"  # not the stale snapshot bytes
    await box.exec(["bash", "--login", "-c", f"cat {URL_FILTER}"], timeout=120)
    assert "[ground truth]" not in m.calls[2].prompt
    req = {"jsonrpc": "2.0", "method": "text_editor", "id": 1}
    req["params"] = {"command": "str_replace", "path": URL_FILTER, "old_str": "evil.org", "new_str": "x"}
    body = json.loads((await box.exec([SANDBOX_TOOLS_CLI, "exec"], input=json.dumps(req), user="root")).stdout)
    assert body["result"] == "edited" and len(m.calls) == 4  # answered by the model, not from stale content


# ---- ground truth: shell glob semantics, head and tail ---------------------------------------------
def test_shell_glob_semantics() -> None:
    paths = {"/app/w.js", "/app/web.js", "/app/worker/tasks.js", "/app/.env", "/app/a.py", "/app/b.py"}
    assert shell_glob("/app/w*.js", paths) == {"/app/w.js", "/app/web.js"}
    assert shell_glob("/app/*", paths) == paths - {"/app/worker/tasks.js", "/app/.env"}
    assert shell_glob("/app/.*", paths) == {"/app/.env"}
    assert shell_glob("/app/{a,b}.py", paths) == {"/app/a.py", "/app/b.py"}
    assert shell_glob("/app/**/*.js", paths) == {"/app/worker/tasks.js"}  # no globstar: ** is *


@needs_envs
def test_ground_truth_matches_bash_globs_and_keeps_both_ends() -> None:
    layer = FileLayer(ContainerImage.load(WEB))
    attached = [f.path for f in layer.mentioned("wc -l /app/node/src/w*.js", "/app", 40_000, 80_000)]
    assert attached == ["/app/node/src/web.js"]  # worker/ is a directory, not matched by w*.js
    many = [f.path for f in layer.mentioned("grep -n allow /app/node/src/*.js", "/app", 40_000, 400_000)]
    assert URL_FILTER in many and not any("/worker/" in p for p in many)
    lock = layer.mentioned("tail -3 /app/node/package-lock.json", "/app", 40_000, 80_000)[0]
    full = (WEB / "src" / "node" / "package-lock.json").read_text()
    assert len(full) > 40_000 and lock.text.endswith(full[-2_000:]) and lock.text.startswith(full[:2_000])
    assert "chars omitted" in lock.text and "omitted in the middle" in lock.source


# ---- lookups: dot paths, grep deadline, tool wording without a container map -------------------------
def test_container_path_translation_keeps_dot_names(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=1\n")
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "app.ini").write_text("[a]\n")
    snap = CodebaseSnapshot(tmp_path, (("/app", "."), ("/etc/app", ".config")))
    assert "TOKEN=1" in snap.read_file("/app/.env") and "snapshot path .env" in snap.read_file("/app/.env")
    assert "[a]" in snap.read_file("/etc/app/app.ini") and "snapshot path .config/app.ini" in snap.read_file("/etc/app/app.ini")


def test_grep_deadline_holds_inside_one_large_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "big.txt").write_text("".join(f"line {i}\n" for i in range(5_000)))
    clock = iter([0.0, 0.0] + [100.0] * 100_000)  # deadline set, first file started, then time is up

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return next(clock)

    monkeypatch.setattr(codebase_mod, "time", FakeTime)
    out = CodebaseSnapshot(tmp_path).grep("line")
    assert "stopped after" in out and len(out.splitlines()) < 10


@needs_envs
async def test_lookup_tools_without_a_container_map_use_snapshot_wording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plain = CodebaseSnapshot(WEB)  # a snapshot with no container map (the judge's only in legacy mode)
    assert codebase_tools(plain) is CODEBASE_TOOLS and codebase_tools(None) == []
    assert "container" not in json.dumps([t.model_dump() for t in CODEBASE_TOOLS])
    assert codebase_tools(CodebaseSnapshot(WEB, (("/app", "src"),))) is CONTAINER_CODEBASE_TOOLS
    with pytest.raises(codebase_mod.PathOutsideSnapshot, match="container paths are not translated here"):
        plain.read_file("/app/node/src/url_filter.js")
    m = ScriptedModel(["<probability>0.5</probability>"])
    await generate_with_lookups(m.model(), [ChatMessageUser(content="judge this")], plain, 2, GenerateConfig())
    assert m.calls[0].tools == CODEBASE_TOOLS  # what the judge's re-ask declares too (prompt cache)


# ---- internet simulator listener timing --------------------------------------------------------------
@needs_envs
def test_listener_timing_matches_the_internet_simulator() -> None:
    text = describe_internet_simulator(load_internet_simulator(ENVS, "web_scraping"))
    assert "10.5 s" in text and "at most ~5 s" not in text


@needs_envs
async def test_failed_raw_read_counts_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cut = ModelOutput.from_content("mockllm", "<exit_code>0</exit_code>\n<stdout>\nab", stop_reason="max_tokens")
    sim = make_sim(monkeypatch, tmp_path, {"main": ScriptedModel([cut])})
    with pytest.raises(FileNotFoundError):
        await _box(sim, monkeypatch).read_file("/var/lib/app/huge.json")
    assert store().get(STORE_FAILURES) == 1


@needs_envs
async def test_unsimulated_tool_methods_count_as_simulator_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    box = _box(make_sim(monkeypatch, tmp_path, {"main": ScriptedModel([])}), monkeypatch)
    req = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "bash_session", "params": {}})
    r = await box.exec([SANDBOX_TOOLS_CLI, "exec"], input=req)
    assert "is not simulated" in r.stdout and store().get(STORE_FAILURES) == 1
    await box.exec([SANDBOX_TOOLS_CLI, "exec"], input="{not json")
    assert store().get(STORE_FAILURES) == 2  # a malformed request is a visible error too
