from pathlib import Path

import pytest
from inspect_ai.tool import ToolCall

import simrt.codebase as codebase_mod
from simrt.codebase import CodebaseSnapshot, PathOutsideSnapshot
from simrt.llm_tools import run_codebase_tool


@pytest.fixture
def snap(tmp_path: Path) -> CodebaseSnapshot:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\nSECRET = 'x'\nprint(SECRET)\n")
    (tmp_path / "Dockerfile").write_text("FROM ubuntu:22.04\nCOPY src/ /app/\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("SECRET")
    return CodebaseSnapshot(tmp_path)


def test_read_file_numbers_lines(snap: CodebaseSnapshot) -> None:
    out = snap.read_file("src/app.py", 2, 2)
    assert "     2\tSECRET = 'x'" in out
    assert "import os" not in out


@pytest.mark.parametrize("bad", ["../etc/passwd", "src/../../x", "/../../etc"])
def test_rejects_escape(snap: CodebaseSnapshot, bad: str) -> None:
    with pytest.raises(PathOutsideSnapshot):
        snap.read_file(bad)


def test_absolute_paths_are_relative_to_root(snap: CodebaseSnapshot) -> None:
    assert "SECRET" in snap.read_file("/src/app.py")


def test_grep_skips_vendor_dirs(snap: CodebaseSnapshot) -> None:
    out = snap.grep("SECRET")
    assert "src/app.py:2" in out
    assert "node_modules" not in out


def test_list_dir(snap: CodebaseSnapshot) -> None:
    out = snap.list_dir(".")
    assert "src/" in out and "Dockerfile" in out and "node_modules" not in out


# ---- lookup fixes: container paths, explicit negatives, containment, bounds -----------------------
def _call(fn: str, **args: object) -> ToolCall:
    return ToolCall(id="1", function=fn, arguments=dict(args))


def test_container_paths_are_translated(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\nSECRET = 'x'\n")
    snap = CodebaseSnapshot(tmp_path, (("/app", "src"),))
    assert "SECRET" in snap.read_file("/app/app.py") and "snapshot path src/app.py" in snap.read_file("/app/app.py")
    assert "src/app.py:2" in snap.grep("SECRET", "/app")
    assert "src/app.py" in snap.list_dir("/app")


def test_paths_outside_snapshot_are_explicit(snap: CodebaseSnapshot) -> None:
    for fn, args in [("grep", {"pattern": "x", "path": "/app/x"}), ("read_file", {"path": "/opt/y"}), ("list_dir", {"path": "/srv"})]:
        out = run_codebase_tool(snap, _call(fn, **args))
        assert out.startswith("error:") and "not in the snapshot" in out and "snapshot root" in out
    assert snap.grep("SECRET", "no/such/dir") == "error: no such file or directory in snapshot: no/such/dir"
    assert snap.grep("NOPE") == "(no matches in 2 files under .)"


def test_read_file_negative_end_line_reads_to_end(snap: CodebaseSnapshot) -> None:
    out = snap.read_file("src/app.py", 2, -1)
    assert "SECRET = 'x'" in out and "print(SECRET)" in out


def test_grep_skips_symlinks_out_of_snapshot(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("HOST-ONLY-CONTENT\n")
    root = tmp_path / "vendor" / "codebase"  # a parent named like a skip dir must not hide everything
    root.mkdir(parents=True)
    (root / "a.txt").write_text("HOST-ONLY-CONTENT is not here but this line matches HOST\n")
    (root / "link.txt").symlink_to(outside)
    out = CodebaseSnapshot(root).grep("HOST")
    assert "a.txt:1" in out and "link.txt" not in out and "HOST-ONLY-CONTENT\n" not in out


def test_grep_marks_cut_lines_and_bounds_regex(snap: CodebaseSnapshot, monkeypatch: pytest.MonkeyPatch) -> None:
    (snap.root / "src" / "long.py").write_text("x = '" + "a" * 500 + "'\n")
    assert "... [+" in snap.grep("aaaa", "src/long.py")

    class SlowRegex:
        def compile(self, pattern: str) -> "SlowRegex":
            return self

        def search(self, line: str, timeout: float) -> None:
            raise TimeoutError

    monkeypatch.setattr(codebase_mod, "_rx", SlowRegex())
    assert "regex timed out" in snap.grep("(a+)+$", "src/app.py")


def test_dispatcher_catches_os_errors(snap: CodebaseSnapshot) -> None:
    out = run_codebase_tool(snap, _call("read_file", path="a" * 300 + "/x"))
    assert out.startswith("error:")
