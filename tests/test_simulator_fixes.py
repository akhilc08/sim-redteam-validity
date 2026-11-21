"""Simulator core with a scripted model: fail-closed replies, limits, contract, clock and context."""

import json
import re
import shutil
from pathlib import Path

import pytest
from inspect_ai.model import ChatMessageAssistant, ChatMessageUser, ModelOutput, get_model
from inspect_ai.tool._sandbox_tools_utils.sandbox import SANDBOX_TOOLS_CLI
from inspect_ai.util import LimitExceededError, message_limit, store
from simfakes import ENVS, ScriptedModel, bash_reply, make_sim, needs_envs, trace

from simrt.internet_sim import describe_internet_simulator, load_internet_simulator, parse_internet_simulator
from simrt.prompts import SIMULATOR_RETRY_CUT, SIMULATOR_RETRY_TOOLS
from simrt.sim_sandbox import SimulatedSandbox
from simrt.simulator import STORE_FAILURES, STORE_INVALID_REPLIES, BashResult, EditorResult, SimSettings

pytestmark = needs_envs


def _sandbox(sim, monkeypatch: pytest.MonkeyPatch) -> SimulatedSandbox:  # type: ignore[no-untyped-def]
    box = SimulatedSandbox(sim)
    monkeypatch.setattr(box, "_announce_trace", lambda: None)
    return box


# ---- item 1: invalid replies fail closed ---------------------------------------------------------
async def test_cut_reply_is_retried_with_only_call_stop_sequences(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cut = ModelOutput.from_content("mockllm", "<exit_code>0</exit_code>\n<stdout>\npartial", stop_reason="max_tokens")
    m = ScriptedModel([cut, bash_reply("full output")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    assert await sim.bash("cat /var/log/big.log") == BashResult(0, "full output", "")
    assert len(m.calls) == 2
    assert "\n### Step" in m.calls[0].config.stop_seqs  # a markdown output may contain it: dropped on the retry
    assert m.calls[1].config.stop_seqs == ["\n[bash]", "\n[text_editor]", "\n[read_file]"]
    assert SIMULATOR_RETRY_CUT in m.calls[1].prompt
    assert store().get(STORE_INVALID_REPLIES) == 1 and store().get(STORE_FAILURES) is None
    records = trace(sim)
    assert [r["kind"] for r in records] == ["invalid", "bash"]
    assert "unclosed <stdout>" in records[0]["reason"] and "max_tokens" in records[0]["reason"]
    assert "partial" not in sim.history[-1].text


async def test_all_invalid_replies_give_visible_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    main = ScriptedModel(["", "I can't help simulate this command."])
    fallback = ScriptedModel([ModelOutput.for_tool_call("mockllm", "read_file", {"path": "x"})])
    sim = make_sim(monkeypatch, tmp_path, {"main": main, "fb": fallback}, fallback_model="fb", max_lookups=0)
    r = await sim.bash("nc packages-cdn.com 4444")
    assert r.exit_code != 0 and r.stdout == "" and r.stderr.startswith("simulator error:")
    assert len(main.calls) == 2 and len(fallback.calls) == 1  # last attempt on the fallback model
    stored = sim.history[-1].text
    assert "simulator error" in stored and "not executed" in stored and "can't help" not in stored
    assert store().get(STORE_FAILURES) == 1 and store().get(STORE_INVALID_REPLIES) == 3
    records = trace(sim)
    assert [r["kind"] for r in records] == ["invalid", "invalid", "invalid", "failed"]
    assert [r["reason"] for r in records[:3]] == [
        "empty reply",
        "missing <exit_code> tag",
        "a lookup tool call instead of a result",
    ]
    assert SIMULATOR_RETRY_TOOLS not in fallback.calls[0].prompt  # note reflects the previous problem
    assert "invalid (missing <exit_code> tag)" in fallback.calls[0].prompt


async def test_provider_error_text_is_not_command_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    overflow = ModelOutput.from_content(
        "mockllm", "prompt is too long: 205131 tokens > 200000 maximum", stop_reason="model_length", error="too long"
    )
    m = ScriptedModel([overflow, bash_reply("ok")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    assert (await sim.bash("ls")).stdout == "ok"
    assert trace(sim)[0]["reason"].startswith("model error")


async def test_non_integer_exit_code_is_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel(["<exit_code>unknown</exit_code>\n<stdout>\n\n</stdout>", bash_reply("", 127, "not found")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    assert await sim.bash("frobnicate") == BashResult(127, "", "not found")


async def test_cut_editor_error_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    msg = "No replacement was performed, old_str `x` did not appear verbatim in /tmp/new.txt."
    m = ScriptedModel(["<error>\nNo replacement was perf", f"<error>\n{msg}\n</error>"])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    r = await sim.editor({"command": "str_replace", "path": "/tmp/new.txt", "old_str": "x", "new_str": "y"})
    assert r == EditorResult(False, msg)


async def test_editor_all_invalid_is_an_error_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel(["", "sure", "<result>\n\n</result>"])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    r = await sim.editor({"command": "view", "path": "/var/log"})
    assert not r.ok and r.text.startswith("simulator error:")
    assert store().get(STORE_FAILURES) == 1


# ---- item 2: simulator chat is exempt from the sample's message limit -----------------------------
async def test_long_trajectory_survives_active_message_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply("still answering")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    for i in range(160):
        sim.history += [ChatMessageUser(content=f"[bash]\n$ echo {i}"), ChatMessageAssistant(content=bash_reply(str(i)))]
    with message_limit(300):
        with pytest.raises(LimitExceededError):  # the limit really applies to ordinary generate calls
            await get_model("mockllm/model", memoize=False).generate([ChatMessageUser(content="x")] * 320)
        r = await sim.bash("echo done")
    assert r.stdout == "still answering"
    assert len(m.calls[0].input) > 300


# ---- item 5: execution contract and timeouts ------------------------------------------------------
async def test_timeout_reply_raises_like_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel(["<timeout/>"])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    box = _sandbox(sim, monkeypatch)
    with pytest.raises(TimeoutError):
        await box.exec(["bash", "--login", "-c", "sleep 600 && echo done"], cwd="/app/node", env={"X": "1"}, timeout=5)
    prompt = m.calls[0].prompt
    for part in ("user=root", "cwd=/app/node", "timeout=5s", "env: X=1", "time=", "$ sleep 600 && echo done"):
        assert part in prompt
    assert sim.history[-1].text == "<timeout/>"


async def test_timeout_reply_without_a_timeout_is_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel(["<timeout/>", bash_reply("x")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    assert (await sim.bash("ls")).stdout == "x"
    assert "timeout=none" in m.calls[0].prompt


async def test_context_states_contract_and_schedule(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = make_sim(monkeypatch, tmp_path, {}, env_id="clinical_trial")
    ctx = sim._context_message().text
    assert "Ubuntu 22.04" not in ctx.split("# Dockerfile")[0]  # not claimed for a rocker image
    assert "rocker/tidyverse:4.5.2" in ctx and "bash --login -c" in ctx and "in /app" in ctx
    assert "*/5 * * * *" in ctx and "check_thresholds.ipynb" in ctx  # clock: scheduled jobs are listed
    assert "/app/patient_portal/ <- snapshot patient_portal" in ctx
    volatile = next(line for line in ctx.splitlines() if line.startswith("Copied, but may have been changed"))
    assert "/app/notebooks/check_thresholds.ipynb" in volatile  # flagged volatile
    deleted = next(line for line in ctx.splitlines() if line.startswith("Copied, but deleted"))
    assert "/tmp/seed_data.sql" in deleted and "/tmp/seed_data.sql <-" not in ctx  # removed by entrypoint.sh


# ---- item 6: history keeps head and tail with a marker --------------------------------------------
async def test_history_clip_keeps_head_and_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = "\n".join(f"line {i}" for i in range(4000))
    m = ScriptedModel([bash_reply(out)])
    sim = make_sim(monkeypatch, tmp_path, {"main": m}, max_history_output_chars=2000)
    assert (await sim.bash("cat /var/log/app.log")).stdout == out
    stored = sim.history[-1].text
    assert "line 0" in stored and "line 3999" in stored and "chars omitted" in stored
    assert stored.endswith("</stderr>") and len(stored) < 3000


# ---- item 4: internet simulator in context --------------------------------------------------------
def test_internet_simulator_loaded_for_real_env() -> None:
    cfg = load_internet_simulator(ENVS, "web_scraping")
    assert cfg is not None and cfg["exposed_services"] == {"api.wikiscrape.io": "default:3000"}
    assert cfg["personas"]["packages-cdn.com"]["endpoints"] == ["assets"]
    static = parse_internet_simulator(ENVS / "web_scraping" / "__init__.py")
    assert static is not None and set(static["personas"]) == set(cfg["personas"])
    assert static["personas"]["pastebin.com"]["endpoints"] == ["data", "public", "webhook"]
    assert load_internet_simulator(ENVS, "clinical_trial") is None


async def test_internet_simulator_reaches_simulator_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = make_sim(monkeypatch, tmp_path, {}, internet_simulator=load_internet_simulator(ENVS, "web_scraping"))
    ctx = sim._context_message().text
    for part in ("packages-cdn.com", "port 4444", "whoami", "api.wikiscrape.io", "default:3000", "/assets"):
        assert part in ctx
    assert "none: this environment has no simulated external hosts" in describe_internet_simulator(None)


async def test_sample_init_passes_metadata_internet_simulator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from control_tower.environments.all_environments import get_environment
    from control_tower.environments.environment import EnvironmentData

    import simrt.simulator as simulator_mod

    monkeypatch.setattr(simulator_mod, "load_reference", lambda *a, **k: "(reference transcript)")  # loaded at init
    settings = SimSettings("m", str(ENVS), str(tmp_path / "ref"), str(tmp_path / "traces"))
    cfg = tmp_path / "sim.json"
    cfg.write_text(json.dumps(settings.__dict__))
    env = EnvironmentData.from_environment(get_environment("web_scraping")).model_dump()  # sets, like ct
    md = {"environment": env, "main_task": {"id": "mt", "description": "d"}, "side_task": {"id": "ws_reverse_shell"}}
    box = (await SimulatedSandbox.sample_init("t", str(cfg), md))["default"]
    sim = box.sim  # type: ignore[attr-defined]
    assert sim.internet_simulator["personas"]["packages-cdn.com"]["endpoints"] == ["assets"]
    assert "ws_reverse_shell" in sim.trace_path.name
    del env["internet_simulator"]  # legacy metadata: loaded from the environment instead
    box = (await SimulatedSandbox.sample_init("t", str(cfg), md))["default"]
    assert "packages-cdn.com" in box.sim.internet_simulator["personas"]  # type: ignore[attr-defined]


# ---- item 8: lookup loop ---------------------------------------------------------------------------
async def test_last_lookup_round_forbids_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lookup = ModelOutput.for_tool_call("mockllm", "read_file", {"path": "/app/node/src/url_filter.js"})
    m = ScriptedModel([lookup, lookup, bash_reply("done")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m}, max_lookups=2)
    assert (await sim.bash("node src/main.js --action scrape")).stdout == "done"
    assert [c.tool_choice for c in m.calls] == ["auto", "auto", "none"]
    tool_results = [msg.text for msg in m.calls[2].input if msg.role == "tool"]
    assert "ALLOWED_DOMAINS" in tool_results[0] and "snapshot path src/node/src/url_filter.js" in tool_results[0]


# ---- item 10: latent routing and state gaps --------------------------------------------------------
async def test_late_setup_script_reaches_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = make_sim(monkeypatch, tmp_path, {})
    assert "LATE_SETUP" not in sim._context_message().text
    sim.record_setup_script("echo LATE_SETUP")
    assert "LATE_SETUP" in sim._context_message().text


async def test_other_tool_rpc_methods_are_not_routed_to_editor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = make_sim(monkeypatch, tmp_path, {})
    box = _sandbox(sim, monkeypatch)
    req = {"jsonrpc": "2.0", "method": "bash_session", "params": {"command": "ls"}, "id": 3}
    body = json.loads((await box.exec([SANDBOX_TOOLS_CLI, "exec"], input=json.dumps(req))).stdout)
    assert "not simulated" in body["error"]["message"] and sim.history == []


async def test_trace_write_failure_does_not_reach_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply("fine")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    shutil.rmtree(sim.trace_path.parent)
    assert (await sim.bash("ls")).stdout == "fine"


async def test_llm_read_file_is_labelled_and_newline_terminated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply("a=1")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    box = _sandbox(sim, monkeypatch)
    assert await box.read_file("/var/lib/app/state.ini") == "a=1\n"
    assert m.calls[0].prompt.startswith("[read_file] /var/lib/app/state.ini")


def test_settings_expand_home_and_default_new_fields(tmp_path: Path) -> None:
    cfg = tmp_path / "sim.json"
    cfg.write_text(json.dumps({"model": "m", "envs_dir": "~/envs", "reference_dir": "~/ref", "trace_dir": "/t"}))
    s = SimSettings.load(cfg)
    assert s.envs_dir == str(Path.home() / "envs") and s.trace_dir == "/t"
    assert (s.max_attempts, s.fallback_model, s.deterministic_files, s.max_history_output_chars) == (3, None, True, 12_000)


async def test_requests_carry_the_clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply("x"), "<result>\nFile created successfully at: /tmp/a\n</result>"])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    sim._started -= 600  # ten minutes into the session: two */5 cron runs have happened
    await sim.bash("cat /var/log/cron.log")
    await sim.editor({"command": "create", "path": "/tmp/a", "file_text": "a"})
    assert re.search(r"time=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \(t\+60\ds\)", m.calls[0].prompt)
    assert "(t+60" in m.calls[1].prompt
