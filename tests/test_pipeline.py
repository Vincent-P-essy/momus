from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import SAMPLE_DIFF, FakeLLM, finding_payload, make_call, tool_turn
from momus.agent.tools import SUBMIT_FINDINGS, SUBMIT_VERDICT
from momus.config import MomusConfig
from momus.errors import BudgetExceededError, ConfigError, DiffError
from momus.github import PRInfo
from momus.llm import UsageTally
from momus.pipeline import _is_ignored, local_diff, run_review

PR = PRInfo(
    number=7,
    title="Add encoding parameter",
    body="",
    head_sha="abc123",
    base_ref="main",
    head_ref="feat",
    author="octocat",
    draft=False,
)


def _submission(*findings: dict[str, Any]) -> Any:
    return tool_turn(
        make_call(SUBMIT_FINDINGS, call_id="s1", summary="Tight change.", findings=list(findings))
    )


def _verdict(verdict: str, rebuttal: str = "checked") -> Any:
    return tool_turn(
        make_call(
            SUBMIT_VERDICT,
            call_id="v1",
            verdict=verdict,
            rebuttal=rebuttal,
            revised_severity=None,
            revised_confidence=None,
            revised_body=None,
        )
    )


def test_end_to_end_review_with_verification(sample_repo: Path, cfg: MomusConfig) -> None:
    tally = UsageTally()
    llm = FakeLLM(
        tally=tally,
        script=[
            tool_turn(make_call("read_file", path="src/app.py", start_line=None, end_line=None)),
            _submission(
                finding_payload(line=3, severity="major", title="Bug A"),
                finding_payload(line=5, severity="minor", category="convention", title="Style B"),
            ),
            _verdict("uphold"),  # Bug A is verified first (worst severity first)
            _verdict("reject", rebuttal="a linter already enforces this"),
        ],
    )
    result = run_review(sample_repo, PR, SAMPLE_DIFF, cfg, llm=llm, tally=tally)

    assert [f.title for f in result.findings] == ["Bug A"]
    assert [r.finding.title for r in result.rejected] == ["Style B"]
    assert result.rejected[0].rebuttal == "a linter already enforces this"
    assert "Bug A" in llm.requests[2].messages[0]["content"][0]["text"]

    stats = result.stats
    assert (stats.candidate_findings, stats.published_findings, stats.rejected_findings) == (
        2,
        1,
        1,
    )
    assert (stats.files_reviewed, stats.additions, stats.deletions) == (1, 3, 2)
    assert stats.model_calls == 4
    assert stats.input_tokens == 400
    assert stats.cost_usd is not None and stats.cost_usd > 0
    assert stats.duration_seconds >= 0


def test_verification_can_be_disabled(sample_repo: Path) -> None:
    cfg = MomusConfig(verify=False, max_iterations=4)
    llm = FakeLLM(script=[_submission(finding_payload(title="Kept as-is"))])
    result = run_review(sample_repo, PR, SAMPLE_DIFF, cfg, llm=llm)
    assert [f.title for f in result.findings] == ["Kept as-is"]
    assert result.rejected == []


def test_ignored_files_short_circuit(sample_repo: Path, cfg: MomusConfig) -> None:
    lock_diff = SAMPLE_DIFF.replace("src/app.py", "poetry.lock")
    result = run_review(sample_repo, PR, lock_diff, cfg, llm=FakeLLM(script=[]))
    assert "No reviewable changes" in result.summary
    assert result.findings == []


def test_min_severity_threshold(sample_repo: Path) -> None:
    cfg = MomusConfig(verify=False, min_severity="major", max_iterations=4)
    llm = FakeLLM(
        script=[
            _submission(
                finding_payload(line=3, severity="major", title="big"),
                finding_payload(line=5, severity="nit", category="docs", title="small"),
            )
        ]
    )
    result = run_review(sample_repo, PR, SAMPLE_DIFF, cfg, llm=llm)
    assert [f.title for f in result.findings] == ["big"]
    assert result.rejected[0].finding.title == "small"
    assert "min_severity" in result.rejected[0].rebuttal


def test_budget_exhaustion_rejects_unverified(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            _submission(
                finding_payload(line=3, severity="major", title="first"),
                finding_payload(line=5, severity="minor", category="testing", title="second"),
            ),
            BudgetExceededError("budget reached"),
        ]
    )
    result = run_review(sample_repo, PR, SAMPLE_DIFF, cfg, llm=llm)
    assert result.findings == []
    assert len(result.rejected) == 2
    assert all("budget" in r.rebuttal for r in result.rejected)


def test_guidelines_are_injected(sample_repo: Path) -> None:
    (sample_repo / "CONTRIBUTING.md").write_text("Always use context managers.\n")
    cfg = MomusConfig(verify=False, guidelines="CONTRIBUTING.md", max_iterations=4)
    llm = FakeLLM(script=[_submission()])
    run_review(sample_repo, PR, SAMPLE_DIFF, cfg, llm=llm)
    first_prompt = llm.requests[0].messages[0]["content"][0]["text"]
    assert "Always use context managers." in first_prompt


def test_missing_guidelines_fail_loudly(sample_repo: Path) -> None:
    cfg = MomusConfig(guidelines="STYLE.md")
    with pytest.raises(ConfigError, match=r"STYLE\.md"):
        run_review(sample_repo, PR, SAMPLE_DIFF, cfg, llm=FakeLLM(script=[]))


def test_is_ignored_patterns() -> None:
    patterns = ["**/*.lock", "**/dist/**"]
    assert _is_ignored("poetry.lock", patterns)
    assert _is_ignored("pkg/sub/poetry.lock", patterns)
    assert _is_ignored("web/dist/bundle.js", patterns)
    assert not _is_ignored("src/app.py", patterns)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_local_diff_against_head(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "f.py").write_text("x = 2\n")

    pr, diff_text = local_diff(tmp_path, None)
    assert pr.title.startswith("Local changes")
    assert "-x = 1" in diff_text
    assert "+x = 2" in diff_text


def _git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout


def test_local_diff_includes_untracked_files(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "brand_new.py").write_text("y = 2\n")

    _, without = local_diff(tmp_path, None)
    assert "brand_new.py" not in without

    _, with_untracked = local_diff(tmp_path, None, include_untracked=True)
    assert "brand_new.py" in with_untracked
    assert "+y = 2" in with_untracked
    # The file is untracked again afterwards, not left registered in the index.
    assert "?? brand_new.py" in _git_out(tmp_path, "status", "--porcelain")


def test_include_untracked_preserves_staged_state(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    # The user deliberately staged a change before running the review.
    (tmp_path / "f.py").write_text("x = 1\nz = 3\n")
    _git(tmp_path, "add", "f.py")
    (tmp_path / "brand_new.py").write_text("y = 2\n")

    _, diff_text = local_diff(tmp_path, None, include_untracked=True)
    assert "brand_new.py" in diff_text
    assert _git_out(tmp_path, "diff", "--cached", "--name-only").split() == ["f.py"]


def test_include_untracked_is_ignored_for_commit_diffs(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "one")
    (tmp_path / "f.py").write_text("x = 2\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "two")
    base = _git_out(tmp_path, "rev-parse", "HEAD~1").strip()
    (tmp_path / "brand_new.py").write_text("y = 2\n")

    _, diff_text = local_diff(tmp_path, base, include_untracked=True)
    assert "+x = 2" in diff_text
    assert "brand_new.py" not in diff_text
    # No index manipulation happened at all for a commit-to-commit diff.
    assert "?? brand_new.py" in _git_out(tmp_path, "status", "--porcelain")


def test_local_diff_errors_without_git_history(tmp_path: Path) -> None:
    with pytest.raises(DiffError):
        local_diff(tmp_path, None)
