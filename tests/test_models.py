from __future__ import annotations

import pytest

from conftest import make_finding
from momus.models import (
    Confidence,
    Finding,
    ReviewResult,
    Severity,
    Verdict,
    Verification,
)


def test_marker_id_is_stable_and_content_addressed() -> None:
    a = make_finding()
    b = make_finding(line=7)  # same path/category/title, different anchor
    c = make_finding(title="Different title")
    assert a.marker_id() == b.marker_id()
    assert a.marker_id() != c.marker_id()
    assert len(a.marker_id()) == 16


def test_finding_rejects_empty_title() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        make_finding(title="   ")


def test_finding_rejects_non_positive_line() -> None:
    with pytest.raises(ValueError, match="1-based"):
        make_finding(line=0)


def test_worst_severity() -> None:
    result = ReviewResult(
        summary="s",
        findings=[make_finding(severity="minor"), make_finding(severity="blocker", line=5)],
    )
    assert result.worst_severity() is Severity.BLOCKER
    assert ReviewResult(summary="s").worst_severity() is None


def test_verification_apply_only_on_revise() -> None:
    finding = make_finding()
    uphold = Verification(verdict=Verdict.UPHOLD, rebuttal="r", revised_severity=Severity.NIT)
    assert uphold.apply_to(finding) is finding

    revise = Verification(
        verdict=Verdict.REVISE,
        rebuttal="overstated",
        revised_severity=Severity.NIT,
        revised_confidence=Confidence.LOW,
        revised_body="  ",  # blank bodies are not applied
    )
    updated = revise.apply_to(finding)
    assert updated.severity is Severity.NIT
    assert updated.confidence is Confidence.LOW
    assert updated.body == finding.body


def test_sort_key_orders_by_severity_then_location() -> None:
    findings = [
        make_finding(severity="nit", line=1, title="z"),
        make_finding(severity="blocker", line=9, title="a"),
    ]
    ordered = sorted(findings, key=Finding.sort_key)
    assert ordered[0].severity is Severity.BLOCKER
