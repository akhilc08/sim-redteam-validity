"""Inspect sandbox whose every command is answered by an LLM simulator instead of a container.

Routing of `exec` calls:
- inspect tool-support JSON-RPC (`<SANDBOX_TOOLS_CLI> exec`, used by text_editor) -> simulator.editor
- task setup scripts (`bash -l -c /tmp/script.sh` after write_file) -> recorded as state, no LLM call
- agent bash (`bash --login -c CMD`) and anything else -> simulator.bash
"""

import json
import shlex
from typing import Any, Literal, Union, overload, override

from inspect_ai.tool._sandbox_tools_utils.sandbox import SANDBOX_TOOLS_CLI
from inspect_ai.util import ExecResult, SandboxEnvironment, sandboxenv, store

from simrt.simulator import SimSettings, Simulator

SETUP_SCRIPT_PATH = "/tmp/script.sh"
SIM_SANDBOX_NAME = "simulated"
TOOL_EXCEPTION_CODE = -32099


def _ok(stdout: str = "", stderr: str = "", code: int = 0) -> ExecResult[str]:
    return ExecResult(success=code == 0, returncode=code, stdout=stdout, stderr=stderr)


def _jsonrpc(req_id: object, result: str | None = None, error: str | None = None) -> str:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        body["error"] = {"code": TOOL_EXCEPTION_CODE, "message": error}
    else:
        body["result"] = result
    return json.dumps(body)


def _metadata_text(metadata: dict[str, Any], key: str, field: str) -> str:
    value = metadata.get(key) or {}
    return str(value.get(field) or "") if isinstance(value, dict) else ""


@sandboxenv(name=SIM_SANDBOX_NAME)
class SimulatedSandbox(SandboxEnvironment):
    def __init__(self, simulator: Simulator) -> None:
        self.sim = simulator
        self._pending_script: str | None = None
        self._trace_announced = False

    # ---- lifecycle ----------------------------------------------------------------------
    @classmethod
    async def sample_init(
        cls, task_name: str, config: Any, metadata: dict[str, str]
    ) -> dict[str, SandboxEnvironment]:
        settings = SimSettings.load(str(config))
        md: dict[str, Any] = dict(metadata)
        env = md.get("environment") or {}
        sim = Simulator(
            settings=settings,
            env_id=str(env.get("id")),
            env_description=str(env.get("description") or ""),
            main_task_id=_metadata_text(md, "main_task", "id") or None,
            main_task_description=_metadata_text(md, "main_task", "description"),
        )
        return {"default": cls(sim)}

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: Any,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        return None

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> Any:
        return config

    def _announce_trace(self) -> None:
        if not self._trace_announced:
            store().set("sim_trace_path", str(self.sim.trace_path))
            self._trace_announced = True

    # ---- exec ---------------------------------------------------------------------------
    @override
    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        self._announce_trace()
        stdin = input.decode(errors="replace") if isinstance(input, bytes) else input

        if cmd[:2] == [SANDBOX_TOOLS_CLI, "exec"]:
            return await self._editor_rpc(stdin or "{}")
        if cmd and cmd[0] == SANDBOX_TOOLS_CLI:
            return _ok()  # version / health probes of the tool-support binary
        if cmd == ["chmod", "+x", SETUP_SCRIPT_PATH] or cmd == ["rm", SETUP_SCRIPT_PATH]:
            return _ok()
        if cmd[:3] == ["bash", "-l", "-c"] and cmd[3:] == [SETUP_SCRIPT_PATH]:
            if self._pending_script is not None:
                self.sim.record_setup_script(self._pending_script)
                self._pending_script = None
            return _ok()

        if cmd[:3] == ["bash", "--login", "-c"] and len(cmd) == 4:
            command = cmd[3]
        else:
            command = shlex.join(cmd)
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        r = await self.sim.bash(command, stdin=stdin, user=user)
        return _ok(r.stdout, r.stderr, r.exit_code)

    async def _editor_rpc(self, raw: str) -> ExecResult[str]:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            return _ok(_jsonrpc(None, error="malformed request"))
        params = req.get("params") or {}
        r = await self.sim.editor(params if isinstance(params, dict) else {"params": params})
        return _ok(_jsonrpc(req.get("id"), result=r.text if r.ok else None, error=None if r.ok else r.text))

    # ---- files --------------------------------------------------------------------------
    @override
    async def write_file(self, file: str, contents: str | bytes) -> None:
        text = contents.decode(errors="replace") if isinstance(contents, bytes) else contents
        if file == SETUP_SCRIPT_PATH:
            self._pending_script = text
            return
        await self.sim.write_file(file, text)

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    @override
    async def read_file(self, file: str, text: bool = True) -> Union[str, bytes]:
        if file == SANDBOX_TOOLS_CLI:
            return "" if text else b""  # pretend the tool-support binary is installed
        r = await self.sim.bash(f"cat {shlex.quote(file)}")
        if r.exit_code != 0:
            raise FileNotFoundError(r.stderr or file)
        return r.stdout if text else r.stdout.encode()
