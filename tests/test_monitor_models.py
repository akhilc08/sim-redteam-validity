"""The monitoring CLI keeps one monitor model per pass tree (<out dir>/mon)."""

import json
import sys
from pathlib import Path

import pytest
from test_pipeline_fixes import fake_ct, write_eval

import simrt.monitoring as mon


def cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["monitoring", *args])
    mon.main()


def test_second_model_in_the_same_out_dir_is_refused_not_marked_stale(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    write_eval(tmp_path / "evals/a.eval")
    fake_ct(monkeypatch, [(8.0, 8.0)])
    cli(monkeypatch, "--evals", str(tmp_path / "evals"), "--model", "anthropic/claude-haiku-5-5", "--n-runs", "1",
        "--out", str(tmp_path / "L/scores.json"))
    ct = fake_ct(monkeypatch, [])  # any ct call fails the test
    with pytest.raises(SystemExit):
        cli(monkeypatch, "--evals", str(tmp_path / "evals"), "--model", "anthropic/claude-sonnet-5-5", "--n-runs", "1",
            "--out", str(tmp_path / "L/other.json"))
    assert ct.calls == [] and not list((tmp_path / "L/mon").glob("*/run_0.stale-*"))  # first fix: moved aside
    fake_ct(monkeypatch, [(3.0, 3.0)])  # its own --out directory works, and the same model can rescore its own
    cli(monkeypatch, "--evals", str(tmp_path / "evals"), "--model", "anthropic/claude-sonnet-5-5", "--n-runs", "1",
        "--out", str(tmp_path / "S/scores.json"))
    assert [v["sus"] for v in json.loads((tmp_path / "S/scores.json").read_text()).values()] == [3.0]
    ct = fake_ct(monkeypatch, [])
    cli(monkeypatch, "--evals", str(tmp_path / "evals"), "--model", "anthropic/claude-haiku-5-5", "--n-runs", "1",
        "--out", str(tmp_path / "L/scores.json"))
    assert ct.calls == [] and [v["sus"] for v in json.loads((tmp_path / "L/scores.json").read_text()).values()] == [8.0]
