"""SCE-style LLM environment simulator: one persistent chat per trajectory.

Fail-closed: every reply is validated (stop reason, required tags present and closed, non-empty, no
pending tool call). An invalid reply is retried with a corrective note, up to `max_attempts`, the
last attempt on `fallback_model` if set. If all fail, the agent sees a visible "simulator error",
history records a neutral note instead of the invalid text, the trace logs each invalid reply, and
the sample store counts the failure (STORE_FAILURES) so downstream code can flag the round.

Files whose content is known exactly are served by simrt.file_layer without asking the model: raw
reads, text_editor calls whose outcome they decide, and plain bash reads (FileLayer.serve_read). Other
bash commands that name such a file go to the model with the file attached as ground truth.
"""

import asyncio
import json
import logging
import posixpath
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    get_model,
)
from inspect_ai.util import store

from simrt.codebase import CodebaseSnapshot
from simrt.container_map import ContainerImage
from simrt.file_layer import FileLayer
from simrt.internet_sim import describe_internet_simulator
from simrt.llm_tools import ToolLoopResult, generate_with_lookups
from simrt.prompts import (
    BASH_FORMAT,
    EDITOR_FORMAT,
    SIMULATOR_CONTEXT,
    SIMULATOR_CONTRACT,
    SIMULATOR_FAILED_NOTE,
    SIMULATOR_GROUND_TRUTH,
    SIMULATOR_RETRY_CUT,
    SIMULATOR_RETRY_CUT_EXACT,
    SIMULATOR_RETRY_FORMAT,
    SIMULATOR_RETRY_TOOLS,
    SIMULATOR_SYSTEM,
)
from simrt.reference import load_reference
from simrt.sim_replies import (
    BashResult,
    EditorResult,
    bash_failure,
    check_bash,
    check_editor,
    clip_middle,
    editor_failure,
    parse_bash,
    parse_editor,
    render_bash,
    render_editor,
)

__all__ = ["BashResult", "EditorResult", "SimSettings", "Simulator", "parse_bash", "parse_editor"]
__all__ += ["render_bash", "render_editor", "STOP_SEQUENCES", "STORE_FAILURES", "STORE_INVALID_REPLIES"]

logger = logging.getLogger(__name__)
R = TypeVar("R")

MAX_HISTORY_OUTPUT_CHARS = 12_000
# The simulator must answer exactly one call; stop if it starts inventing the next one. A retry after
# a cut-off reply drops "### Step", which a real output (a markdown file) may well contain.
RETRY_STOP_SEQUENCES = ["\n[bash]", "\n[text_editor]", "\n[read_file]"]
STOP_SEQUENCES = [*RETRY_STOP_SEQUENCES, "\n### Step"]
STORE_FAILURES = "simrt_sim_invalid"  # calls that ended in a visible simulator error
STORE_INVALID_REPLIES = "simrt_sim_invalid_replies"  # every rejected reply, retried or not


@dataclass(frozen=True)
class SimSettings:
    model: str
    envs_dir: str
    reference_dir: str
    trace_dir: str
    max_lookups: int = 6
    max_tokens: int = 8_000
    reference_chars: int = 60_000
    max_attempts: int = 3
    fallback_model: str | None = None
    deterministic_files: bool = True  # False reproduces the old all-LLM file handling
    max_history_output_chars: int = MAX_HISTORY_OUTPUT_CHARS
    ground_truth_file_chars: int = 40_000
    ground_truth_total_chars: int = 80_000

    @classmethod
    def load(cls, path: str | Path) -> "SimSettings":
        data = json.loads(Path(path).read_text())
        for key in ("envs_dir", "reference_dir", "trace_dir"):  # committed configs say ~/work/...
            if isinstance(data.get(key), str):
                data[key] = str(Path(data[key]).expanduser())
        return cls(**data)


@dataclass
class Simulator:
    settings: SimSettings
    env_id: str
    env_description: str
    main_task_id: str | None
    main_task_description: str
    setup_scripts: list[str] = field(default_factory=list)
    history: list[ChatMessage] = field(default_factory=list)
    side_task_id: str | None = None
    internet_simulator: dict[str, Any] | None = None
    trace_path: Path = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _context: ChatMessageUser | None = field(default=None, init=False)
    _reference: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._root = Path(self.settings.envs_dir) / self.env_id / "codebase"
        self.image = ContainerImage.load(self._root)
        self.files = FileLayer(self.image, enabled=self.settings.deterministic_files)
        self.snapshot = self._lookup_snapshot()
        self._started = time.time()
        self._notes: list[str] = []
        tdir = Path(self.settings.trace_dir)
        tdir.mkdir(parents=True, exist_ok=True)
        ids = [self.env_id, str(self.main_task_id), *([self.side_task_id] if self.side_task_id else [])]
        self.trace_path = tdir / f"{'__'.join(ids)}__{uuid.uuid4().hex[:8]}.jsonl"

    # ---- state changes that need no LLM call -------------------------------------------
    def record_setup_script(self, script: str) -> None:
        """Setup scripts are recorded, not executed: whatever they could change or remove is marked so."""
        self.setup_scripts.append(script)
        self.files.invalidate(self.image.apply_setup_script(script))
        self.snapshot = self._lookup_snapshot()
        self._context = None  # rebuilt so the script is listed even if recorded late
        self._trace({"kind": "setup", "request": script})

    def _lookup_snapshot(self) -> CodebaseSnapshot:
        img = self.image
        return CodebaseSnapshot(
            self._root, img.lookup_map(), tuple(sorted(img.deleted.items())), frozenset(img.volatile)
        )

    def read_exact(self, path: str) -> bytes | None:
        """Exact bytes of a file the harness knows (no model call), else None."""
        full = path if path.startswith("/") else posixpath.join(self.image.workdir, path)
        data = self.files.raw_bytes(full)
        if data is not None:
            self._notes.append(f"the tool harness read {full} directly (exact content, {len(data)} bytes)")
            self._trace({"kind": "read_exact", "request": full, "bytes": len(data)})
        return data

    async def write_file(self, path: str, contents: str, exact: bool = True) -> None:
        """A raw sandbox write (not via a tool). Record it as a state change in the chat."""
        async with self._lock:
            self.files.record_write(path, contents, exact)
            body = clip_middle(contents, self.settings.max_history_output_chars)
            self._remember(f"[file written directly to {path}]\n{body}", "<exit_code>0</exit_code>")
            self._trace({"kind": "write", "request": path, "chars": len(contents), "content": contents[:100_000]})

    # ---- context ------------------------------------------------------------------------
    def reference(self) -> str:
        """The rendered honest reference transcript, loaded once. Raises simrt.reference.ReferenceUnavailable
        when there is no usable one (SimulatedSandbox.sample_init calls this, so the sample fails before
        the agent's first model call)."""
        if self._reference is None:
            self._reference = load_reference(
                self.settings.reference_dir, self.main_task_id, self.settings.reference_chars
            )
        return self._reference

    def _context_message(self) -> ChatMessageUser:
        if self._context is None:
            snap, img = self.snapshot, self.image
            dockerfile = snap.read_optional("Dockerfile") or snap.read_optional("src/Dockerfile") or "(not found)"
            reference = self.reference()
            self._context = ChatMessageUser(
                content=SIMULATOR_CONTEXT.format(
                    env_description=self.env_description,
                    main_task=self.main_task_description,
                    contract=SIMULATOR_CONTRACT.format(os=img.os_description(), user=img.user, workdir=img.workdir),
                    dockerfile=dockerfile,
                    compose=snap.read_optional("compose.yml") or "(not found)",
                    path_map=_describe_paths(img),
                    network=describe_internet_simulator(self.internet_simulator),
                    schedule=_describe_schedule(img),
                    tree=snap.list_dir(".", depth=2),
                    reference=reference,
                    setup="\n\n".join(f"```bash\n{s}\n```" for s in self.setup_scripts) or "(none)",
                )
            )
        return self._context

    def _clock(self) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"time={now} (t+{int(time.time() - self._started)}s)"

    # ---- simulation ---------------------------------------------------------------------
    async def _generate_valid(
        self, kind: str, request: str, extra: str, check: Callable[[ToolLoopResult], tuple[R | None, str | None]]
    ) -> tuple[R | None, str, int]:
        """Ask the model until `check` accepts a reply: (result or None, last problem, attempts used)."""
        system = ChatMessageSystem(
            content=f"{SIMULATOR_SYSTEM}\n\n## bash tool\n{BASH_FORMAT}\n\n## text_editor tool\n{EDITOR_FORMAT}"
        )
        problem: str | None = None
        attempts = used = max(1, self.settings.max_attempts)
        for attempt in range(1, attempts + 1):
            model_name = self.settings.model
            if attempt == attempts > 1 and self.settings.fallback_model:  # SCE: second model on the last try
                model_name = self.settings.fallback_model
            note = f"\n\n{_retry_note(problem, kind)}" if problem else ""
            msgs = [system, self._context_message(), *self.history, ChatMessageUser(content=request + extra + note)]
            stop = RETRY_STOP_SEQUENCES if problem and "cut off" in problem else STOP_SEQUENCES
            config = GenerateConfig(max_tokens=self.settings.max_tokens, cache_prompt=True, stop_seqs=stop)
            t0 = time.time()
            res = await generate_with_lookups(get_model(model_name), msgs, self.snapshot, self.settings.max_lookups, config)
            result, problem = check(res)
            self._trace(
                {
                    "kind": kind if problem is None else "invalid",
                    "step": kind,
                    "attempt": attempt,
                    "model": model_name,
                    "request": request,
                    "response": res.text,
                    "reason": problem,
                    "stop_reason": res.stop_reason,
                    "lookups": res.lookups,
                    "seconds": round(time.time() - t0, 2),
                    "usage": [u.model_dump(exclude_none=True) for u in res.usage],
                }
            )
            if problem is None:
                return result, "", attempt
            _count(STORE_INVALID_REPLIES)
            if kind == "read_file" and "cut off" in problem and res.stop_reason == "max_tokens":
                used = attempt  # a raw read is never shortened, so a retry would be cut off again
                break
        _count(STORE_FAILURES)
        self._trace({"kind": "failed", "step": kind, "request": request, "reason": problem, "attempts": used})
        return None, problem or "invalid reply", used

    def _check(
        self, res: ToolLoopResult, parse: Callable[[str], tuple[R | None, str | None]]
    ) -> tuple[R | None, str | None]:
        """Reject provider errors, refusals and pending tool calls, then parse the reply text."""
        if res.error:
            return None, f"model error: {res.error[:200]}"
        if res.stop_reason == "model_length":
            return None, "input too long for the model (context overflow)"
        if res.stop_reason == "content_filter":
            return None, "refused (content filter)"
        if res.tool_calls_pending:
            return None, "a lookup tool call instead of a result"
        result, problem = parse(res.text)
        if problem and res.stop_reason == "max_tokens":
            problem += f"; hit max_tokens={self.settings.max_tokens}"
        return result, problem

    async def bash(
        self,
        command: str,
        stdin: str | None = None,
        user: str | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
    ) -> BashResult:
        head = f"[bash] user={user or self.image.user} cwd={cwd or self.image.workdir} "
        head += f"timeout={f'{timeout}s' if timeout else 'none'}{'' if timeout_retry else ' (no retry)'} {self._clock()}"
        request = f"{head}\n$ {command}"
        if env:
            request += "\nenv: " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in sorted(env.items()))
        if stdin:
            request += f"\n[stdin]\n{stdin}"
        plain = not stdin and not env and (user or self.image.user) == "root"  # how ct's bash tool calls
        return await self._run_bash("bash", request, command, cwd or self.image.workdir, timeout is not None, plain)

    async def read_file(self, path: str) -> BashResult:
        """A raw file read the file layer cannot serve: ask the model for the file's content."""
        request = f"[read_file] {path} {self._clock()}"
        return await self._run_bash("read_file", request, None, self.image.workdir, False)

    async def _run_bash(
        self, kind: str, request: str, command: str | None, cwd: str, can_time_out: bool, plain: bool = False
    ) -> BashResult:
        async with self._lock:
            request = self._with_notes(request)
            exact = self.files.serve_read(command, cwd) if plain and command else None
            if exact is not None:  # a plain read of known files (cat, head, ...): exact, no model call
                self.files.after_bash(command, cwd)  # type: ignore[arg-type]
                result = BashResult(0, exact, "")
                self._remember(request, render_bash(result, self.settings.max_history_output_chars))
                self._trace({"kind": "bash", "deterministic": True, "request": request, "chars": len(exact),
                             "response": exact[:100_000]})
                return result
            extra, attached = self._ground_truth(command, cwd) if command else ("", [])
            result, problem, attempts = await self._generate_valid(
                kind, request, extra, lambda res: self._check(res, lambda t: check_bash(t, can_time_out))
            )
            if command is not None:
                self.files.after_bash(command, cwd)
            stored = request + (f"\n[ground truth for {', '.join(attached)} was attached]" if attached else "")
            cap = self.settings.max_history_output_chars
            if result is None:
                result = bash_failure(f"no valid result after {attempts} attempt(s): {problem}")
                note = SIMULATOR_FAILED_NOTE.format(reason=problem)
                self._remember(stored, render_bash(BashResult(1, "", f"{result.stderr}\n{note}"), cap))
            else:
                self._remember(stored, render_bash(result, cap))
            return result

    async def editor(self, params: dict[str, object], user: str | None = None) -> EditorResult:
        async with self._lock:
            body = json.dumps(params, ensure_ascii=False, indent=1)
            request = self._with_notes(f"[text_editor]{f' user={user}' if user else ''} {self._clock()}\n{body}")
            cap = self.settings.max_history_output_chars
            if (user or self.image.user) == "root":
                outcome = self.files.editor(params)
                if outcome is not None:  # outcome fully determined by known content: no model call
                    result = EditorResult(outcome.ok, outcome.text)
                    self._remember(request, render_editor(result, cap))
                    self._trace({"kind": "editor", "deterministic": True, "request": request, "response": result.text})
                    return result
            result, problem, attempts = await self._generate_valid(
                "editor", request, "", lambda res: self._check(res, check_editor)
            )
            self.files.after_model_editor(params, None if result is None else result.ok)
            if result is None:
                result = editor_failure(f"no valid result after {attempts} attempt(s): {problem}")
                note = SIMULATOR_FAILED_NOTE.format(reason=problem)
                self._remember(request, render_editor(EditorResult(False, f"{result.text}\n{note}"), cap))
            else:
                self._remember(request, render_editor(result, cap))
            return result

    # ---- helpers ------------------------------------------------------------------------
    def _ground_truth(self, command: str, cwd: str) -> tuple[str, list[str]]:
        files = self.files.mentioned(
            command, cwd, self.settings.ground_truth_file_chars, self.settings.ground_truth_total_chars
        )
        if not files:
            return "", []
        blocks = [f'<file path="{f.path}" source="{f.source}">\n{f.text}\n</file>' for f in files]
        return "\n\n" + SIMULATOR_GROUND_TRUTH.format(count=len(files)) + "\n" + "\n".join(blocks), [f.path for f in files]

    def _with_notes(self, request: str) -> str:
        if not self._notes:
            return request
        notes = "\n".join(f"[harness] {n}" for n in self._notes)
        self._notes.clear()
        return f"{notes}\n{request}"

    def _remember(self, request: str, answer: str) -> None:
        self.history.append(ChatMessageUser(content=request))
        self.history.append(ChatMessageAssistant(content=answer))

    def _trace(self, record: dict[str, object]) -> None:
        record = {"t": time.time(), **record}
        try:
            with self.trace_path.open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as e:  # a lost trace line must not become an agent-visible tool error
            logger.warning("simulator trace write to %s failed: %s", self.trace_path, e)

    def settings_dict(self) -> dict[str, object]:
        return asdict(self.settings)


def _retry_note(problem: str, kind: str) -> str:
    if "cut off" in problem:  # only a bash output may be shortened (Inspect hides the omission line)
        return SIMULATOR_RETRY_CUT if kind == "bash" else SIMULATOR_RETRY_CUT_EXACT
    if "tool call" in problem:
        return SIMULATOR_RETRY_TOOLS
    return SIMULATOR_RETRY_FORMAT.format(reason=problem)


def _count(key: str) -> None:
    s = store()
    s.set(key, int(s.get(key, 0)) + 1)


def _describe_paths(img: ContainerImage) -> str:
    lines = [
        f"- {container}{'/' if img.root and (img.root / snap).is_dir() else ''} <- snapshot {snap}"
        for container, snap in img.copies
        if not img.is_deleted(container)
    ]
    if img.volatile:
        lines.append(
            "Copied, but may have been changed (or removed) at start-up, by a setup script or by scheduled "
            "jobs, so the snapshot shows only their original content: " + ", ".join(sorted(img.volatile)[:40])
        )
    if img.deleted:
        lines.append(
            "Copied, but deleted before the agent started (at start-up or by a setup script), so they do NOT "
            "exist in the container: " + ", ".join(sorted(img.deleted)[:40])
        )
    return "\n".join(lines) or "(none found: map paths with the Dockerfile and compose file)"


def _describe_schedule(img: ContainerImage) -> str:
    startup = "; ".join(f"`{c.strip()}`" for c in img.startup) or "(see the Dockerfile CMD/ENTRYPOINT)"
    cron = "\n".join(f"- {line}" for line in img.cron) or "- none found in the image"
    return (
        f"Start-up (ran when the container started, before the agent's first call): {startup}\n"
        f"Cron jobs in the image (they keep running during the session):\n{cron}"
    )
