from __future__ import annotations

from typing import Any

from rich.console import Console

from conftest import make_finding
from momus.config import MomusConfig
from momus.errors import GitHubError
from momus.github import PRInfo
from momus.models import Evidence, RejectedFinding, ReviewResult, ReviewStats
from momus.publish import render_to_terminal
from momus.publish.github import (
    SUMMARY_MARKER,
    finding_marker,
    publish_to_github,
    render_comment_body,
    render_summary_body,
    to_review_comment,
)

PR = PRInfo(
    number=7,
    title="t",
    body="",
    head_sha="abc123",
    base_ref="main",
    head_ref="feat",
    author="octocat",
    draft=False,
)


class StubGitHub:
    def __init__(
        self,
        comments: list[dict[str, Any]] | None = None,
        reviews: list[dict[str, Any]] | None = None,
        fail_first_create: bool = False,
    ) -> None:
        self.comments = comments or []
        self.reviews = reviews or []
        self.created: list[dict[str, Any]] = []
        self._fail_first = fail_first_create

    def list_review_comments(self, number: int) -> list[dict[str, Any]]:
        return self.comments

    def list_reviews(self, number: int) -> list[dict[str, Any]]:
        return self.reviews

    def create_review(
        self,
        number: int,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
        event: str = "COMMENT",
    ) -> dict[str, Any]:
        if self._fail_first:
            self._fail_first = False
            raise GitHubError("422: line must be part of the diff")
        self.created.append({"commit_id": commit_id, "body": body, "comments": comments})
        return {}


def _result(**overrides: Any) -> ReviewResult:
    defaults: dict[str, Any] = {
        "summary": "Small, focused change.",
        "findings": [make_finding()],
        "stats": ReviewStats(
            files_reviewed=1,
            additions=3,
            deletions=2,
            published_findings=1,
            model_calls=4,
            cost_usd=0.12,
            duration_seconds=42.0,
        ),
    }
    defaults.update(overrides)
    return ReviewResult.model_validate(defaults)


def test_comment_body_contains_marker_and_metadata() -> None:
    finding = make_finding(suggestion="with open(path) as fh:\n    data = fh.read()")
    body = render_comment_body(finding)
    assert finding_marker(finding) in body
    assert "**File handle is never closed**" in body
    assert "```suggestion" in body
    assert "with open(path) as fh:" in body


def test_comment_body_truncates_long_evidence() -> None:
    finding = make_finding()
    finding.evidence[0] = Evidence(kind="repo", location="a.py:1", excerpt="x" * 500)
    body = render_comment_body(finding)
    assert "x" * 200 + "…" in body
    assert "x" * 201 not in body


def test_review_comment_range_fields() -> None:
    comment = to_review_comment(make_finding(line=5, start_line=3))
    assert comment["line"] == 5
    assert comment["start_line"] == 3
    assert comment["start_side"] == "RIGHT"
    assert "start_line" not in to_review_comment(make_finding())


def test_publish_posts_new_findings() -> None:
    gh = StubGitHub()
    outcome = publish_to_github(gh, PR, _result(), MomusConfig())  # type: ignore[arg-type]
    assert "posted a review with 1 inline comment(s)" in outcome
    (created,) = gh.created
    assert created["commit_id"] == "abc123"
    assert len(created["comments"]) == 1
    assert SUMMARY_MARKER in created["body"]


def test_publish_skips_duplicates_entirely() -> None:
    finding = make_finding()
    gh = StubGitHub(
        comments=[{"body": f"something {finding_marker(finding)} something"}],
        reviews=[{"body": f"{SUMMARY_MARKER}\nolder run"}],
    )
    outcome = publish_to_github(gh, PR, _result(), MomusConfig())  # type: ignore[arg-type]
    assert "nothing new to post" in outcome
    assert gh.created == []


def test_publish_no_findings_posts_summary_once() -> None:
    gh = StubGitHub()
    result = _result(findings=[])
    outcome = publish_to_github(gh, PR, result, MomusConfig())  # type: ignore[arg-type]
    assert "0 inline comment(s)" in outcome
    assert len(gh.created) == 1

    gh_second = StubGitHub(reviews=[{"body": gh.created[0]["body"]}])
    outcome = publish_to_github(gh_second, PR, result, MomusConfig())  # type: ignore[arg-type]
    assert outcome == "nothing new to post"


def test_publish_falls_back_when_anchors_are_stale() -> None:
    gh = StubGitHub(fail_first_create=True)
    outcome = publish_to_github(gh, PR, _result(), MomusConfig())  # type: ignore[arg-type]
    assert "inline anchors were stale" in outcome
    (created,) = gh.created
    assert created["comments"] == []
    assert "File handle is never closed" in created["body"]


def test_summary_body_sections() -> None:
    result = _result(
        unanchored=[make_finding(title="Ghost finding")],
        rejected=[RejectedFinding(finding=make_finding(title="Rejected one"), rebuttal="nope")],
    )
    body = render_summary_body(result, MomusConfig())
    assert "| 🟠 major | 1 |" in body
    assert "Ghost finding" in body
    assert "rejected during verification" in body.lower()
    assert "$0.12" in body
    assert "momus v" in body


def test_summary_body_without_cost_footer() -> None:
    body = render_summary_body(_result(), MomusConfig(cost_footer=False))
    assert "model calls" not in body


def test_terminal_rendering_smoke() -> None:
    console = Console(record=True, width=100)
    result = _result(
        unanchored=[make_finding(title="Ghost")],
        rejected=[RejectedFinding(finding=make_finding(title="Weak"), rebuttal="unproven")],
    )
    render_to_terminal(result, console=console)
    text = console.export_text()
    assert "Momus review" in text
    assert "File handle is never closed" in text
    assert "Weak" in text
    assert "$0.12" in text
