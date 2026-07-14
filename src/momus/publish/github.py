"""Turn a ReviewResult into a GitHub review: inline comments + summary body.

Every comment embeds an invisible marker derived from the finding, so re-runs
on the same PR skip findings that are already posted instead of spamming
duplicates. The summary body carries its own marker so a "no findings" review
is only posted once.
"""

from __future__ import annotations

import re
from typing import Any

from momus import __version__
from momus.config import MomusConfig
from momus.github import GitHubClient, PRInfo
from momus.models import Finding, ReviewResult, Severity

_MARKER_RE = re.compile(r"<!-- momus:([0-9a-f]{16}) -->")
SUMMARY_MARKER = "<!-- momus:summary -->"

_SEVERITY_BADGE = {
    Severity.BLOCKER: "🛑",
    Severity.MAJOR: "🟠",
    Severity.MINOR: "🟡",
    Severity.NIT: "⚪",
}


def finding_marker(finding: Finding) -> str:
    return f"<!-- momus:{finding.marker_id()} -->"


def render_comment_body(finding: Finding) -> str:
    lines = [
        finding_marker(finding),
        f"{_SEVERITY_BADGE[finding.severity]} **{finding.title}**",
        f"`{finding.category.value}` · severity **{finding.severity.value}** · "
        f"confidence {finding.confidence.value}",
        "",
        finding.body.strip(),
    ]
    for evidence in finding.evidence:
        excerpt = evidence.excerpt.strip().replace("\n", " ")
        if len(excerpt) > 200:
            excerpt = excerpt[:200] + "…"
        lines.append(f"> {evidence.kind.value} · `{evidence.location}` — {excerpt}")
    if finding.suggestion is not None and finding.suggestion.strip():
        lines += ["", "```suggestion", finding.suggestion.rstrip("\n"), "```"]
    return "\n".join(lines)


def to_review_comment(finding: Finding) -> dict[str, Any]:
    comment: dict[str, Any] = {
        "path": finding.path,
        "line": finding.line,
        "side": finding.side.value,
        "body": render_comment_body(finding),
    }
    if finding.start_line is not None:
        comment["start_line"] = finding.start_line
        comment["start_side"] = finding.side.value
    return comment


def render_summary_body(result: ReviewResult, cfg: MomusConfig) -> str:
    stats = result.stats
    parts = [SUMMARY_MARKER, "## Momus review", "", result.summary.strip()]

    if result.findings:
        parts += ["", "| severity | count |", "|---|---|"]
        for severity in Severity:
            count = sum(1 for f in result.findings if f.severity is severity)
            if count:
                parts.append(f"| {_SEVERITY_BADGE[severity]} {severity.value} | {count} |")

    if result.unanchored:
        parts += ["", "**Findings that could not be anchored to a diff line:**"]
        for finding in result.unanchored:
            parts.append(
                f"- {_SEVERITY_BADGE[finding.severity]} **{finding.title}** "
                f"({finding.path}:{finding.line}) — {finding.body.strip()}"
            )

    if result.rejected:
        parts += [
            "",
            f"<sub>{len(result.rejected)} candidate finding(s) were rejected during "
            "verification and not posted.</sub>",
        ]

    if cfg.cost_footer:
        cost = f"${stats.cost_usd:.2f}" if stats.cost_usd is not None else "cost n/a"
        parts += [
            "",
            "---",
            f"<sub>{stats.files_reviewed} file(s) reviewed (+{stats.additions} "
            f"-{stats.deletions}) · {stats.published_findings} finding(s) posted, "
            f"{stats.rejected_findings} rejected · {stats.model_calls} model calls · "
            f"{cost} · {stats.duration_seconds:.0f}s · momus v{__version__}</sub>",
        ]
    return "\n".join(parts)


def _already_posted_ids(gh: GitHubClient, pr_number: int) -> set[str]:
    ids: set[str] = set()
    for comment in gh.list_review_comments(pr_number):
        ids.update(_MARKER_RE.findall(str(comment.get("body") or "")))
    return ids


def _summary_already_posted(gh: GitHubClient, pr_number: int) -> bool:
    return any(
        SUMMARY_MARKER in str(review.get("body") or "") for review in gh.list_reviews(pr_number)
    )


def publish_to_github(
    gh: GitHubClient,
    pr: PRInfo,
    result: ReviewResult,
    cfg: MomusConfig,
) -> str:
    """Post the review; returns a one-line human description of what happened."""
    posted_ids = _already_posted_ids(gh, pr.number)
    fresh = [f for f in result.findings if f.marker_id() not in posted_ids]
    duplicates = len(result.findings) - len(fresh)

    if not fresh and _summary_already_posted(gh, pr.number):
        return (
            f"nothing new to post ({duplicates} finding(s) already on the PR)"
            if duplicates
            else "nothing new to post"
        )

    # Anchors can go stale if the PR moved since the diff was fetched; if GitHub
    # rejects the batch, retry once with the comments folded into the body.
    body = render_summary_body(result, cfg)
    comments = [to_review_comment(f) for f in fresh]
    try:
        gh.create_review(pr.number, pr.head_sha, body, comments)
    except Exception:
        if not comments:
            raise
        fallback = (
            body
            + "\n\n"
            + "\n\n".join(f"**{f.path}:{f.line}**\n{render_comment_body(f)}" for f in fresh)
        )
        gh.create_review(pr.number, pr.head_sha, fallback, [])
        return f"posted {len(fresh)} finding(s) in the review body (inline anchors were stale)"

    suffix = f", {duplicates} duplicate(s) skipped" if duplicates else ""
    return f"posted a review with {len(fresh)} inline comment(s){suffix}"
