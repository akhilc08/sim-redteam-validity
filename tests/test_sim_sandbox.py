import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from inspect_ai.tool._sandbox_tools_utils.sandbox import SANDBOX_TOOLS_CLI

import simrt.sim_sandbox as sb
from simrt.judge import parse_probability
from simrt.simulator import BashResult, EditorResult, parse_bash, parse_editor


@dataclass
class FakeSim:
    bash_calls: list[tuple[str, str | None]] = field(default_factory=list)
    editor_calls: list[dict[str, Any]] = field(default_factory=list)
    setups: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    editor_reply: EditorResult = EditorResult(True, "File created successfully at: /a")
    trace_path: str = "/tmp/trace.jsonl"

    async def bash(self, command: str, stdin: str | None = None, user: str | None = None) -> BashResult:
        self.bash_calls.append((command, stdin))
        return BashResult(1 if "missing" in command else 0, f"out:{command}", "")

    async def editor(self, params: dict[str, Any]) -> EditorResult:
        self.editor_calls.append(params)
        return self.editor_reply

    def record_setup_script(self, script: str) -> None:
        self.setups.append(script)

    async def write_file(self, path: str, contents: str) -> None:
        self.writes.append(path)


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> tuple[sb.SimulatedSandbox, FakeSim]:
    fake = FakeSim()
    sandbox = sb.SimulatedSandbox(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(sandbox, "_announce_trace", lambda: None)
    return sandbox, fake


async def test_agent_bash_is_unwrapped(box: tuple[sb.SimulatedSandbox, FakeSim]) -> None:
    sandbox, fake = box
    r = await sandbox.exec(["bash", "--login", "-c", "ls /app"])
    assert fake.bash_calls == [("ls /app", None)]
    assert r.success and r.stdout == "out:ls /app"


async def test_python_tool_passes_stdin(box: tuple[sb.SimulatedSandbox, FakeSim]) -> None:
    sandbox, fake = box
    await sandbox.exec(["bash", "--login", "-c", "python3 -"], input="print(1)")
    assert fake.bash_calls == [("python3 -", "print(1)")]


async def test_setup_script_recorded_without_llm(box: tuple[sb.SimulatedSandbox, FakeSim]) -> None:
    sandbox, fake = box
    await sandbox.write_file("/tmp/script.sh", "#!/bin/bash\necho hi")
    assert (await sandbox.exec(["chmod", "+x", "/tmp/script.sh"])).success
    assert (await sandbox.exec(["bash", "-l", "-c", "/tmp/script.sh"])).success
    assert (await sandbox.exec(["rm", "/tmp/script.sh"])).success
    assert fake.setups == ["#!/bin/bash\necho hi"]
    assert fake.bash_calls == [] and fake.writes == []


async def test_editor_jsonrpc_roundtrip(box: tuple[sb.SimulatedSandbox, FakeSim]) -> None:
    sandbox, fake = box
    req = {"jsonrpc": "2.0", "method": "text_editor", "params": {"command": "create", "path": "/a"}, "id": 7}
    r = await sandbox.exec([SANDBOX_TOOLS_CLI, "exec"], input=json.dumps(req))
    assert fake.editor_calls == [{"command": "create", "path": "/a"}]
    assert json.loads(r.stdout) == {"jsonrpc": "2.0", "id": 7, "result": "File created successfully at: /a"}


async def test_editor_error_uses_tool_exception_code(box: tuple[sb.SimulatedSandbox, FakeSim]) -> None:
    sandbox, fake = box
    fake.editor_reply = EditorResult(False, "The path /x does not exist. Please provide a valid path.")
    req = {"jsonrpc": "2.0", "method": "text_editor", "params": {"command": "view", "path": "/x"}, "id": 1}
    body = json.loads((await sandbox.exec([SANDBOX_TOOLS_CLI, "exec"], input=json.dumps(req))).stdout)
    assert body["error"]["code"] == -32099 and "does not exist" in body["error"]["message"]


async def test_tool_support_probe_reads_succeed(box: tuple[sb.SimulatedSandbox, FakeSim]) -> None:
    sandbox, fake = box
    assert await sandbox.read_file(SANDBOX_TOOLS_CLI, False) == b""
    assert fake.bash_calls == []


async def test_read_file_missing_raises(box: tuple[sb.SimulatedSandbox, FakeSim]) -> None:
    sandbox, _ = box
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file("/missing")


async def test_cwd_is_prefixed(box: tuple[sb.SimulatedSandbox, FakeSim]) -> None:
    sandbox, fake = box
    await sandbox.exec(["ls"], cwd="/app")
    assert fake.bash_calls[0][0] == "cd /app && ls"


def test_parse_bash_tags() -> None:
    r = parse_bash("<exit_code>2</exit_code>\n<stdout>\nhello\n</stdout>\n<stderr>\noops\n</stderr>")
    assert r == BashResult(2, "hello", "oops")


def test_parse_bash_untagged_falls_back_to_stdout() -> None:
    assert parse_bash("just text") == BashResult(0, "just text", "")


def test_parse_editor() -> None:
    assert parse_editor("<error>\nnope\n</error>") == EditorResult(False, "nope")
    assert parse_editor("<result>\nok\n</result>") == EditorResult(True, "ok")


def test_parse_probability_takes_last_and_clamps() -> None:
    assert parse_probability("<probability>0.2</probability> ... <probability>0.85</probability>") == 0.85
    assert parse_probability("<probability>1.7</probability>") == 1.0
    assert parse_probability("no answer") != parse_probability("no answer")  # nan


def test_runaway_continuation_is_cut_to_first_block() -> None:
    """Regression: simulator once wrote the whole future session after the first answer."""
    from simrt.simulator import render_bash

    raw = (
        "<exit_code>0</exit_code>\n<stdout>\n/app/a.R\n</stdout>\n<stderr>\n\n</stderr>\n\n"
        "[bash] (user=root)\n$ cat /app/a.R\n\n<exit_code>0</exit_code>\n<stdout>\nINVENTED\n</stdout>"
    )
    canon = render_bash(parse_bash(raw))
    assert "/app/a.R" in canon and "INVENTED" not in canon and "[bash]" not in canon


def test_editor_first_block_wins() -> None:
    assert parse_editor("<error>\nbad\n</error>\n<result>\nlater\n</result>") == EditorResult(False, "bad")
    assert parse_editor("<result>\nok\n</result>\n<error>\nlater\n</error>") == EditorResult(True, "ok")
