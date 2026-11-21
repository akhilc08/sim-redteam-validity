"""Inspect sandbox whose every command is answered by an LLM simulator instead of a container.

Routing of `exec` calls:
- inspect tool-support JSON-RPC (`<SANDBOX_TOOLS_CLI> exec`): method text_editor -> simulator.editor;
  other methods are not simulated and return a visible error, counted like the simulator's own
  failures (STORE_FAILURES) so the round is flagged
- task setup scripts (`bash -l -c /tmp/script.sh` after write_file) -> recorded as state, no LLM call
- agent bash (`bash --login -c CMD`) and anything else -> simulator.bash, with the call's cwd, env,
  user and timeout. A simulated timeout raises TimeoutError, as Inspect's docker sandbox does, so
  tools report "Command timed out before completing."
`read_file` of a file whose content the harness knows exactly is served without an LLM call.
sample_init loads the honest reference transcript, so a sample without a usable one errors before the
agent starts (simrt.reference.ReferenceUnavailable).
"""

import json
import shlex
from typing import Any, Literal, Union, overload, override

from anyio import to_thread
from inspect_ai.tool._sandbox_tools_utils.sandbox import SANDBOX_TOOLS_CLI
from inspect_ai.util import ExecResult, SandboxEnvironment, sandboxenv, store

from simrt.internet_sim import as_plain, load_internet_simulator
from simrt.simulator import STORE_FAILURES, SimSettings, Simulator, _count

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
        env_id = str(env.get("id"))
        if isinstance(env, dict) and "internet_simulator" in env:
            internet = as_plain(env["internet_simulator"])
        else:  # older metadata: read the environment's own config
            internet = load_internet_simulator(settings.envs_dir, env_id)
        sim = Simulator(
            settings=settings,
            env_id=env_id,
            env_description=str(env.get("description") or ""),
            main_task_id=_metadata_text(md, "main_task", "id") or None,
            main_task_description=_metadata_text(md, "main_task", "description"),
            side_task_id=_metadata_text(md, "side_task", "id") or None,
            internet_simulator=internet,
        )
        # Without a usable honest reference (ReferenceUnavailable) the sample errors here, before the agent's
        # first model call, instead of at its first simulated tool call. A worker thread reads the logs:
        # read_eval_log inside the running event loop would patch that loop with nest_asyncio.
        await to_thread.run_sync(sim.reference)
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
            return await self._tool_rpc(stdin or "{}", user)
        if cmd and cmd[0] == SANDBOX_TOOLS_CLI:
            return _ok()  # version / health probes of the tool-support binary
        if cmd == ["chmod", "+x", SETUP_SCRIPT_PATH] or cmd == ["rm", SETUP_SCRIPT_PATH]:
            return _ok()
        if cmd[:3] == ["bash", "-l", "-c"] and cmd[3:] == [SETUP_SCRIPT_PATH]:
            if self._pending_script is not None:
                self.sim.record_setup_script(self._pending_script)
                self._pending_script = None
            return _ok()

        command = cmd[3] if cmd[:3] == ["bash", "--login", "-c"] and len(cmd) == 4 else shlex.join(cmd)
        r = await self.sim.bash(
            command, stdin=stdin, user=user, cwd=cwd, env=env, timeout=timeout, timeout_retry=timeout_retry
        )
        if r.timed_out:  # same exception as inspect's docker sandbox after its retries
            raise TimeoutError(f"Docker compose command '{shlex.join(cmd)}' timed out after {timeout} seconds")
        return _ok(r.stdout, r.stderr, r.exit_code)

    async def _tool_rpc(self, raw: str, user: str | None) -> ExecResult[str]:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _count(STORE_FAILURES)
            return _ok(_jsonrpc(None, error="malformed request"))
        method = req.get("method", "text_editor")
        if method != "text_editor":
            _count(STORE_FAILURES)  # the agent sees a simulator error, so the round's sim_failures must too
            return _ok(_jsonrpc(req.get("id"), error=f"simulator error: tool method {method!r} is not simulated"))
        params = req.get("params") or {}
        r = await self.sim.editor(params if isinstance(params, dict) else {"params": params}, user=user)
        return _ok(_jsonrpc(req.get("id"), result=r.text if r.ok else None, error=None if r.ok else r.text))

    # ---- files --------------------------------------------------------------------------
    @override
    async def write_file(self, file: str, contents: str | bytes) -> None:
        exact = True
        if isinstance(contents, bytes):
            try:
                text = contents.decode("utf-8")
            except UnicodeDecodeError:  # binary: the simulator only gets an approximation
                text, exact = contents.decode(errors="replace"), False
        else:
            text = contents
        if file == SETUP_SCRIPT_PATH:
            self._pending_script = text
            return
        await self.sim.write_file(file, text, exact=exact)

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    @override
    async def read_file(self, file: str, text: bool = True) -> Union[str, bytes]:
        if file == SANDBOX_TOOLS_CLI:
            return "" if text else b""  # pretend the tool-support binary is installed
        exact = self.sim.read_exact(file)
        if exact is not None:  # decoded strictly, like the docker sandbox
            return exact.decode("utf-8") if text else exact
        r = await self.sim.read_file(file)
        if r.exit_code != 0 or r.timed_out:
            raise FileNotFoundError(r.stderr or file)
        out = r.stdout if not r.stdout or r.stdout.endswith("\n") else r.stdout + "\n"
        return out if text else out.encode()
