"""SCE-style LLM environment simulator: one persistent chat per trajectory."""

import asyncio
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    get_model,
)

from simrt.codebase import CodebaseSnapshot
from simrt.llm_tools import generate_with_lookups
from simrt.prompts import BASH_FORMAT, EDITOR_FORMAT, SIMULATOR_CONTEXT, SIMULATOR_SYSTEM
from simrt.reference import load_reference

MAX_HISTORY_OUTPUT_CHARS = 12_000


@dataclass(frozen=True)
class SimSettings:
    model: str
    envs_dir: str
    reference_dir: str
    trace_dir: str
    max_lookups: int = 6
    max_tokens: int = 8_000
    reference_chars: int = 60_000

    @classmethod
    def load(cls, path: str | Path) -> "SimSettings":
        return cls(**json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class BashResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class EditorResult:
    ok: bool
    text: str


def _tag(text: str, name: str) -> str | None:
    m = re.search(rf"<{name}>\n?(.*?)\n?</{name}>", text, re.DOTALL)
    return m.group(1) if m else None


def parse_bash(text: str) -> BashResult:
    code_s = _tag(text, "exit_code")
    try:
        code = int((code_s or "0").strip())
    except ValueError:
        code = 0
    stdout = _tag(text, "stdout")
    if stdout is None and code_s is None:
        stdout = text  # model ignored the format; treat everything as stdout
    return BashResult(code, stdout or "", _tag(text, "stderr") or "")


def parse_editor(text: str) -> EditorResult:
    err = _tag(text, "error")
    res = _tag(text, "result")
    if err is not None and (res is None or text.find("<error>") < text.find("<result>")):
        return EditorResult(False, err)
    return EditorResult(True, res if res is not None else text)


def render_bash(r: BashResult) -> str:
    """Canonical form stored in history, so stray model text never leaks into later context."""
    return f"<exit_code>{r.exit_code}</exit_code>\n<stdout>\n{r.stdout}\n</stdout>\n<stderr>\n{r.stderr}\n</stderr>"


def render_editor(r: EditorResult) -> str:
    tag = "result" if r.ok else "error"
    return f"<{tag}>\n{r.text}\n</{tag}>"


# The simulator must answer exactly one call; stop if it starts inventing the next one.
STOP_SEQUENCES = ["\n[bash]", "\n[text_editor]", "\n### Step"]


@dataclass
class Simulator:
    settings: SimSettings
    env_id: str
    env_description: str
    main_task_id: str | None
    main_task_description: str
    setup_scripts: list[str] = field(default_factory=list)
    history: list[ChatMessage] = field(default_factory=list)
    trace_path: Path = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _context: ChatMessageUser | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.snapshot = CodebaseSnapshot(Path(self.settings.envs_dir) / self.env_id / "codebase")
        tdir = Path(self.settings.trace_dir)
        tdir.mkdir(parents=True, exist_ok=True)
        tag = f"{self.env_id}__{self.main_task_id}__{uuid.uuid4().hex[:8]}"
        self.trace_path = tdir / f"{tag}.jsonl"

    # ---- state changes that need no LLM call -------------------------------------------
    def record_setup_script(self, script: str) -> None:
        self.setup_scripts.append(script)
        self._trace({"kind": "setup", "request": script})

    # ---- context ------------------------------------------------------------------------
    def _context_message(self) -> ChatMessageUser:
        if self._context is None:
            snap = self.snapshot
            dockerfile = snap.read_optional("Dockerfile") or snap.read_optional("src/Dockerfile") or "(not found)"
            compose = snap.read_optional("compose.yml") or "(not found)"
            reference = load_reference(
                self.settings.reference_dir, self.main_task_id, self.settings.reference_chars
            )
            setup = "\n\n".join(f"```bash\n{s}\n```" for s in self.setup_scripts) or "(none)"
            self._context = ChatMessageUser(
                content=SIMULATOR_CONTEXT.format(
                    env_description=self.env_description,
                    main_task=self.main_task_description,
                    dockerfile=dockerfile,
                    compose=compose,
                    tree=snap.list_dir(".", depth=2),
                    reference=reference,
                    setup=setup,
                )
            )
        return self._context

    # ---- simulation ---------------------------------------------------------------------
    async def _simulate(self, kind: str, request: str, canonical: Callable[[str], str]) -> str:
        """Run one simulated step; history stores `canonical(raw)` rather than the raw model text."""
        async with self._lock:
            system = ChatMessageSystem(
                content=f"{SIMULATOR_SYSTEM}\n\n## bash tool\n{BASH_FORMAT}\n\n## text_editor tool\n{EDITOR_FORMAT}"
            )
            user = ChatMessageUser(content=request)
            msgs: list[ChatMessage] = [system, self._context_message(), *self.history, user]
            t0 = time.time()
            res = await generate_with_lookups(
                get_model(self.settings.model),
                msgs,
                self.snapshot,
                self.settings.max_lookups,
                GenerateConfig(
                    max_tokens=self.settings.max_tokens, cache_prompt=True, stop_seqs=STOP_SEQUENCES
                ),
            )
            self.history.append(user)
            self.history.append(ChatMessageAssistant(content=canonical(res.text)[:MAX_HISTORY_OUTPUT_CHARS]))
            self._trace(
                {
                    "kind": kind,
                    "request": request,
                    "response": res.text,
                    "lookups": res.lookups,
                    "seconds": round(time.time() - t0, 2),
                    "usage": [u.model_dump(exclude_none=True) for u in res.usage],
                }
            )
            return res.text

    async def bash(self, command: str, stdin: str | None = None, user: str | None = None) -> BashResult:
        req = f"[bash] (user={user or 'root'})\n$ {command}"
        if stdin:
            req += f"\n[stdin]\n{stdin}"
        return parse_bash(await self._simulate("bash", req, lambda t: render_bash(parse_bash(t))))

    async def editor(self, params: dict[str, object]) -> EditorResult:
        req = "[text_editor]\n" + json.dumps(params, ensure_ascii=False, indent=1)
        raw = await self._simulate("editor", req, lambda t: render_editor(parse_editor(t)))
        return parse_editor(raw)

    async def write_file(self, path: str, contents: str) -> None:
        """A raw sandbox write (not via a tool). Record it as a state change in the chat."""
        async with self._lock:
            self.history.append(ChatMessageUser(content=f"[file written directly to {path}]\n{contents[:20_000]}"))
            self.history.append(ChatMessageAssistant(content="<exit_code>0</exit_code>"))
            self._trace({"kind": "write", "request": path, "chars": len(contents)})

    def _trace(self, record: dict[str, object]) -> None:
        record = {"t": time.time(), **record}
        with self.trace_path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def settings_dict(self) -> dict[str, object]:
        return asdict(self.settings)
