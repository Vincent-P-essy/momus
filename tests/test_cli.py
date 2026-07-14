from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from conftest import make_finding
from momus.cli import main
from momus.github import PRInfo
from momus.models import ReviewResult, ReviewStats


def _canned_result(**overrides: Any) -> ReviewResult:
    defaults: dict[str, Any] = {
        "summary": "One real issue.",
        "findings": [make_finding()],
        "stats": ReviewStats(files_reviewed=1, published_findings=1),
    }
    defaults.update(overrides)
    return ReviewResult.model_validate(defaults)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def patched_local(monkeypatch: pytest.MonkeyPatch) -> None:
    pr = PRInfo(0, "Local changes", "", "", "HEAD", "worktree", "", False)
    monkeypatch.setattr("momus.cli.local_diff", lambda root, base: (pr, "diff"))
    monkeypatch.setattr("momus.cli.run_review", lambda root, pr, diff, cfg, **kw: _canned_result())


def test_requires_a_target(runner: CliRunner) -> None:
    result = runner.invoke(main, ["review"])
    assert result.exit_code != 0
    assert "provide --repo" in result.output


def test_local_review_renders_to_terminal(
    runner: CliRunner, patched_local: None, tmp_path: Path
) -> None:
    result = runner.invoke(main, ["review", "--local", "--repo-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Momus review" in result.output
    assert "File handle is never closed" in result.output


def test_local_review_json_output(runner: CliRunner, patched_local: None, tmp_path: Path) -> None:
    result = runner.invoke(main, ["review", "--local", "--json", "--repo-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"] == "One real issue."
    assert payload["findings"][0]["path"] == "src/app.py"


def test_fail_on_severity_gate(runner: CliRunner, patched_local: None, tmp_path: Path) -> None:
    result = runner.invoke(
        main, ["review", "--local", "--fail-on", "major", "--repo-root", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "failing" in result.output


def test_fail_on_ignores_lower_severities(
    runner: CliRunner, patched_local: None, tmp_path: Path
) -> None:
    result = runner.invoke(
        main, ["review", "--local", "--fail-on", "blocker", "--repo-root", str(tmp_path)]
    )
    assert result.exit_code == 0


def test_github_review_requires_token(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    result = runner.invoke(
        main, ["review", "--repo", "octo/demo", "--pr", "7", "--repo-root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "GITHUB_TOKEN" in result.output


def test_draft_pr_is_skipped(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class StubGitHub:
        def __init__(self, token: str, repo: str) -> None: ...

        def get_pr(self, number: int) -> PRInfo:
            return PRInfo(number, "wip", "", "sha", "main", "feat", "octo", draft=True)

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("momus.cli.GitHubClient", StubGitHub)
    result = runner.invoke(
        main, ["review", "--repo", "octo/demo", "--pr", "7", "--repo-root", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "draft pull request" in result.output


def test_invalid_config_surfaces_cleanly(
    runner: CliRunner, patched_local: None, tmp_path: Path
) -> None:
    result = runner.invoke(
        main,
        ["review", "--local", "--repo-root", str(tmp_path), "--max-findings", "0"],
    )
    assert result.exit_code == 1
    assert "invalid configuration" in result.output


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "momus" in result.output
