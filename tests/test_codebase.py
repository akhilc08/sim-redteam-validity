from pathlib import Path

import pytest

from simrt.codebase import CodebaseSnapshot, PathOutsideSnapshot


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
