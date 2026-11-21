"""Source a side-task scorer depends on outside scorer.py, so the success judge sees the real check.

Many scorers are thin wrappers: ws_reverse_shell calls control_tower.environments.scorer_utils.
check_reverse_shell, suppress_safety_notifications imports clinical_trial.utils.scorer_utils, and
suppress_warnings runs a sibling script. Modules are found as files under the environments dir
(environment packages) and under <envs_dir>/../src (control_tower); nothing is imported or run.
"""

import ast
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from simrt.transcript import clip

MAX_CONTEXT_CHARS = 24_000
MIN_CLIPPED_PIECE = 2_000  # a piece that does not fit is clipped to the space left, if at least this much
NOTE_CHARS = 300  # reserved for the note naming what the cap left out
MAX_REEXPORT_HOPS = 2
MAX_CALL_DEPTH = 2  # imported definition -> what it uses -> what those use (same module only)
NOT_SIBLINGS = {"scorer.py", "__init__.py"}
NO_LOGIC = {"linux_scorer"}  # control_tower's marker decorator: no scoring logic to show


@dataclass(frozen=True)
class _Module:
    lines: list[str]
    defs: dict[str, ast.stmt]  # top-level functions, classes and assignments
    imports: dict[str, tuple[str, str]]  # local name -> (module, name) for absolute `from X import`


def _load(path: Path) -> _Module | None:
    try:
        text = path.read_text(errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return None
    defs: dict[str, ast.stmt] = {}
    imports: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defs[node.name] = node
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            for t in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(t, ast.Name):
                    defs[t.id] = node
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                imports[a.asname or a.name] = (node.module, a.name)
    return _Module(text.splitlines(), defs, imports)


def _module_file(module: str, roots: list[Path]) -> tuple[Path, Path] | None:
    """(file, root it was found under) for a dotted module name, or None (stdlib, inspect_ai, ...)."""
    parts = module.split(".")
    for root in roots:
        for cand in (root.joinpath(*parts).with_suffix(".py"), root.joinpath(*parts, "__init__.py")):
            if cand.is_file():
                return cand, root
    return None


def _source(mod: _Module, node: ast.stmt) -> str:
    start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
    return "\n".join(mod.lines[start - 1 : node.end_lineno])


def _helper_pieces(scorer_tree: ast.Module, roots: list[Path]) -> list[tuple[str, str]]:
    """(label, source) for each imported definition, then (breadth first) the same-module definitions
    those use, up to MAX_CALL_DEPTH."""
    queue = deque(
        (node.module, a.name, 0, 0)
        for node in ast.walk(scorer_tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
        for a in node.names
        if a.name not in NO_LOGIC
    )
    modules: dict[Path, _Module | None] = {}
    seen: set[tuple[Path, str]] = set()
    pieces: list[tuple[str, str]] = []
    while queue:
        module, name, hops, depth = queue.popleft()
        found = _module_file(module, roots)
        if found is None:
            continue
        path, root = found
        if path not in modules:
            modules[path] = _load(path)
        mod = modules[path]
        if mod is None or (path, name) in seen:
            continue
        seen.add((path, name))
        node = mod.defs.get(name)
        if node is None:  # re-exported from another module
            if name in mod.imports and hops < MAX_REEXPORT_HOPS:
                queue.append((*mod.imports[name], hops + 1, depth))
            continue
        pieces.append((f"{path.relative_to(root)}: {name}", _source(mod, node)))
        if depth < MAX_CALL_DEPTH:
            used = dict.fromkeys(n.id for n in ast.walk(node) if isinstance(n, ast.Name))
            queue.extend((module, u, hops, depth + 1) for u in used if u in mod.defs and u not in NO_LOGIC)
    return pieces


def scorer_context(side_task_dir: str | Path, envs_dir: str | Path, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Sibling scripts the scorer names, then the source of the helpers it imports, within `max_chars`.

    Returns "" when the scorer depends on nothing outside scorer.py that can be found.
    """
    st_dir = Path(side_task_dir)
    try:
        tree = ast.parse((st_dir / "scorer.py").read_text(errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return ""
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    named = [f for f in sorted(st_dir.iterdir()) if f.is_file() and f.name in literals and f.name not in NOT_SIBLINGS]
    siblings = [
        (f"{f.name} (in the side task directory, run by the scorer)", data.decode(errors="replace"))
        for f in named
        if b"\0" not in (data := f.read_bytes())  # text files only
    ]
    envs = Path(envs_dir).resolve()
    helpers = _helper_pieces(tree, [envs, envs.parent / "src"])
    parts: list[str] = []
    omitted: list[str] = []
    left = max_chars - NOTE_CHARS  # room for the pieces and their "\n\n" separators
    for label, src in siblings + helpers:
        piece = f"# ---- {label} ----\n{src}"
        if len(piece) + 2 > left and left >= MIN_CLIPPED_PIECE:
            piece = clip(piece, left - 100)
        if len(piece) + 2 > left:
            omitted.append(label.split(": ")[-1])
            continue
        parts.append(piece)
        left -= len(piece) + 2
    if omitted:
        parts.append(f"# (not shown, size cap reached: {', '.join(omitted)})"[: NOTE_CHARS - 2])
    return "\n\n".join(parts)
