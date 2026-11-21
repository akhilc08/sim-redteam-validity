"""Real runs re-run a command that times out, and nothing simrt can pass turns that off (README, Limitations).

These tests pin the mechanism in the pinned Inspect (0.3.171) and Control Tower (699d5ca) without docker: the
host-side `docker compose` call is faked. An upgrade that changes it fails here, so the README entry gets
revisited.
"""

import asyncio

import pytest
from inspect_ai.util import ExecResult
from inspect_ai.util._sandbox.docker import compose
from inspect_ai.util._sandbox.docker.docker import DockerSandboxEnvironment
from inspect_ai.util._sandbox.docker.util import ComposeProject
from inspect_ai.util._sandbox.events import SandboxEnvironmentProxy

CMD = ["default", "bash", "--login", "-c", "echo x >> /tmp/log; sleep 999"]


def _fake_docker(monkeypatch: pytest.MonkeyPatch, finish_on: int | None = None) -> list[tuple[list[str], int | None]]:
    """Each host-side `docker compose` call times out until attempt `finish_on`. Returns (args, limit) per call."""
    calls: list[tuple[list[str], int | None]] = []

    async def fake_subprocess(args: list[str], **kw: object) -> ExecResult[str]:
        calls.append((list(args), kw.get("timeout")))  # type: ignore[arg-type]
        if finish_on is None or len(calls) < finish_on:
            raise TimeoutError
        return ExecResult(True, 0, "done\n", "")

    monkeypatch.setattr(compose, "subprocess", fake_subprocess)
    return calls


def _project() -> ComposeProject:
    return ComposeProject("simrt-test", None, None, None, None)


def _exec(timeout_retry: bool = True) -> ExecResult[str]:
    return asyncio.run(compose.compose_exec(CMD, project=_project(), timeout=120, timeout_retry=timeout_retry,
                                            concurrency=False))


@pytest.mark.parametrize(("timeout_retry", "runs"), [(True, [120, 60, 30]), (False, [120])])
def test_a_timed_out_command_runs_up_to_three_times(monkeypatch: pytest.MonkeyPatch, timeout_retry: bool,
                                                    runs: list[int]) -> None:
    """ct's default 120 s limit, then min(T, 60), then min(T, 60) // 2. Only timeout_retry=False runs it once."""
    calls = _fake_docker(monkeypatch)
    with pytest.raises(TimeoutError):
        _exec(timeout_retry)
    assert [limit for _, limit in calls] == runs


def test_a_retry_that_finishes_in_time_is_returned_as_the_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent sees the retry's output and no timeout, where the simulator (one answer) reports a timeout."""
    calls = _fake_docker(monkeypatch, finish_on=2)
    assert _exec().stdout == "done\n" and [limit for _, limit in calls] == [120, 60]


def test_the_real_tool_path_never_turns_the_retry_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """ct's bash tool (the replay policy and the live attacker both get it from get_policy_tools), through
    Inspect's bash tool and sandbox proxy, into the DockerSandboxEnvironment objects that ct's lb-docker
    sample_init returns (Inspect's own class, docker.py:213): a command that keeps timing out is started three
    times, the same command each time."""
    ct_docker = pytest.importorskip("control_tower.sandbox.docker")
    bash_tools = pytest.importorskip("control_tower.tools.bash_tools")
    from inspect_ai.tool._tools import _execute

    assert ct_docker.LBDockerSandboxEnvironment.exec is DockerSandboxEnvironment.exec
    calls = _fake_docker(monkeypatch)
    env = SandboxEnvironmentProxy(DockerSandboxEnvironment("default", _project(), "/"))
    monkeypatch.setattr(_execute, "sandbox_env", lambda name=None: env)
    with pytest.raises(TimeoutError):
        asyncio.run(bash_tools.bash_with_timeout(user="root")(cmd=CMD[-1]))
    assert [limit for _, limit in calls] == [120, 60, 30]
    assert all(args == calls[0][0] for args, _ in calls)
    assert calls[0][0][-5:] == CMD
