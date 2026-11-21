"""Render a simulator settings template (config/sim_*.json) into the file a run actually uses.

- Expands ~ in the path fields, so the committed configs work for any user (they say ~/work/...).
- With --drop-api-route, removes direct Anthropic API routes (anthropic/claude-...) from the model
  strings (model, and fallback_model if set), leaving the Bedrock routes: the API key is optional,
  and without one that route could not even be constructed.
- Fails with a clear message when envs_dir is missing or reference_dir holds no .eval logs,
  instead of letting the simulator run without its codebase or honest reference transcripts.
- With --trace-dir, writes simulator traces somewhere else (keeps one experiment's traces apart).
Unknown fields are copied unchanged.

  python scripts/render_settings.py TEMPLATE OUT [--drop-api-route] [--trace-dir DIR]
"""

import argparse
import json
import sys
from pathlib import Path

PATH_FIELDS = ("envs_dir", "reference_dir", "trace_dir")
MODEL_FIELDS = ("model", "fallback_model")


def is_api_route(route: str) -> bool:
    """A direct Anthropic API route needs ANTHROPIC_API_KEY; Bedrock/Vertex routes do not."""
    model = route.strip().rpartition("@")[0] or route.strip()  # strip a `@region` suffix
    return model.startswith("anthropic/") and not model.startswith(("anthropic/bedrock/", "anthropic/vertex/"))


def drop_api_routes(model: str) -> str:
    prefix, rotating = ("rotate/", True) if model.startswith("rotate/") else ("", False)
    routes = [r for r in model.removeprefix(prefix).split(",") if r.strip() and not is_api_route(r)]
    if not routes:
        raise SystemExit(f"model {model!r} has only direct-API routes; set ANTHROPIC_API_KEY or add Bedrock routes")
    return prefix + ",".join(routes) if rotating else routes[0]


def render(template: dict[str, object], drop_api_route: bool, trace_dir: str | None = None) -> dict[str, object]:
    out = dict(template)
    if trace_dir:
        out["trace_dir"] = trace_dir
    for k in PATH_FIELDS:
        if isinstance(out.get(k), str):
            out[k] = str(Path(str(out[k])).expanduser())
    if drop_api_route:
        for k in MODEL_FIELDS:
            if isinstance(out.get(k), str):
                out[k] = drop_api_routes(str(out[k]))
    return out


def check_inputs(settings: dict[str, object]) -> None:
    envs, ref = Path(str(settings["envs_dir"])), Path(str(settings["reference_dir"]))
    if not envs.is_dir():
        raise SystemExit(f"envs_dir {envs} not found: run scripts/setup_box.sh (clones the environments)")
    if not any(ref.glob("**/*.eval")):
        raise SystemExit(f"reference_dir {ref} has no .eval logs: run `scripts/m1_pipeline.sh honest` first "
                         "(the simulator is grounded in those honest reference transcripts)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("template")
    ap.add_argument("out")
    ap.add_argument("--drop-api-route", action="store_true", help="no API key: keep only Bedrock routes")
    ap.add_argument("--trace-dir", help="override trace_dir")
    a = ap.parse_args()
    settings = render(json.loads(Path(a.template).read_text()), a.drop_api_route, a.trace_dir)
    check_inputs(settings)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(out)
    print(f"sim settings: {out} (model {settings['model']})", file=sys.stderr)


if __name__ == "__main__":
    main()
