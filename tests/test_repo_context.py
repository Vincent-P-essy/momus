from __future__ import annotations

from pathlib import Path

import pytest

from momus.context import RepoContext


@pytest.fixture
def ctx(sample_repo: Path) -> RepoContext:
    return RepoContext(sample_repo)


def test_read_file_numbers_lines(ctx: RepoContext) -> None:
    output = ctx.read_file("src/app.py")
    assert "src/app.py (lines 1-8 of 8)" in output
    assert "    1 | import os" in output
    assert "    4 |     data = open(path, encoding=encoding).read()" in output


def test_read_file_window(ctx: RepoContext) -> None:
    output = ctx.read_file("src/app.py", start_line=3, end_line=4)
    assert "lines 3-4 of 8" in output
    assert "import os" not in output


def test_read_missing_file(ctx: RepoContext) -> None:
    assert "does not exist" in ctx.read_file("src/nope.py")


def test_read_start_past_eof(ctx: RepoContext) -> None:
    assert "only 8 lines" in ctx.read_file("src/app.py", start_line=99)


def test_read_binary_file(ctx: RepoContext, sample_repo: Path) -> None:
    (sample_repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert "looks binary" in ctx.read_file("blob.bin")


def test_path_jail_relative_escape(ctx: RepoContext) -> None:
    assert "outside the repository" in ctx.read_file("../../etc/passwd")


def test_path_jail_absolute_path(ctx: RepoContext) -> None:
    assert "outside the repository" in ctx.read_file("/etc/passwd")


def test_path_jail_symlink_escape(ctx: RepoContext, sample_repo: Path) -> None:
    (sample_repo / "sneaky").symlink_to("/etc")
    assert "outside the repository" in ctx.read_file("sneaky/passwd")


def test_search_finds_matches(ctx: RepoContext) -> None:
    output = ctx.search(r"def load")
    assert "src/app.py:3" in output


def test_search_python_fallback(ctx: RepoContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RepoContext, "_search_ripgrep", lambda self, pattern, glob: None)
    output = ctx.search(r"def load")
    assert "src/app.py:3" in output


def test_search_glob_filter(ctx: RepoContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RepoContext, "_search_ripgrep", lambda self, pattern, glob: None)
    assert "no matches" in ctx.search(r"def load", glob="*.md")


def test_search_invalid_regex(ctx: RepoContext) -> None:
    assert "invalid regex" in ctx.search("([unclosed")


def test_search_no_matches(ctx: RepoContext) -> None:
    assert "no matches" in ctx.search(r"zebra_unicorn_42")


def test_list_dir(ctx: RepoContext) -> None:
    output = ctx.list_dir(".")
    assert "src/" in output
    assert "README.md" in output


def test_list_dir_outside(ctx: RepoContext) -> None:
    assert "outside the repository" in ctx.list_dir("..")


def test_tree_without_git(ctx: RepoContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RepoContext, "_git_tracked_files", lambda self: None)
    output = ctx.tree()
    assert "src/app.py" in output
    assert "README.md" in output
