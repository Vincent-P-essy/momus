"""Typed domain objects: findings, verdicts, review results.

These are the contract between the agent layer (which produces them from model
output) and the publishing layer (which renders them). Validation lives here so
malformed model output is rejected at the boundary.
"""

from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Side(str, Enum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class Category(str, Enum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    API_DESIGN = "api-design"
    CONVENTION = "convention"
    TESTING = "testing"
    DOCS = "docs"


class Severity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


#: Lower rank = more severe. Used for ordering and threshold filtering.
SEVERITY_RANK: dict[Severity, int] = {
    Severity.BLOCKER: 0,
    Severity.MAJOR: 1,
    Severity.MINOR: 2,
    Severity.NIT: 3,
}


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceKind(str, Enum):
    REPO = "repo"
    DIFF = "diff"
    DOC = "doc"


class Evidence(BaseModel):
    """A citation backing a finding: repo precedent, the diff itself, or project docs."""

    kind: EvidenceKind
    location: str
    excerpt: str


class Finding(BaseModel):
    """One review comment candidate, anchored to a diff line."""

    path: str
    side: Side
    line: int
    start_line: int | None = None
    category: Category
    severity: Severity
    confidence: Confidence
    title: str
    body: str
    evidence: list[Evidence] = Field(default_factory=list)
    suggestion: str | None = None

    @field_validator("title", "body", "path")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("line")
    @classmethod
    def _positive_line(cls, value: int) -> int:
        if value < 1:
            raise ValueError("line numbers are 1-based")
        return value

    def marker_id(self) -> str:
        """Stable id used to deduplicate comments across re-runs of the same PR."""
        raw = f"{self.path}|{self.category.value}|{self.title.strip().lower()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def sort_key(self) -> tuple[int, str, int]:
        return (SEVERITY_RANK[self.severity], self.path, self.line)


class Verdict(str, Enum):
    UPHOLD = "uphold"
    REVISE = "revise"
    REJECT = "reject"


class Verification(BaseModel):
    """Outcome of the adversarial verification pass for one finding."""

    verdict: Verdict
    rebuttal: str
    revised_severity: Severity | None = None
    revised_confidence: Confidence | None = None
    revised_body: str | None = None

    def apply_to(self, finding: Finding) -> Finding:
        """Return the finding with any revisions from a `revise` verdict applied."""
        if self.verdict is not Verdict.REVISE:
            return finding
        updates: dict[str, object] = {}
        if self.revised_severity is not None:
            updates["severity"] = self.revised_severity
        if self.revised_confidence is not None:
            updates["confidence"] = self.revised_confidence
        if self.revised_body is not None and self.revised_body.strip():
            updates["body"] = self.revised_body
        return finding.model_copy(update=updates) if updates else finding


class RejectedFinding(BaseModel):
    finding: Finding
    rebuttal: str


class ReviewStats(BaseModel):
    files_reviewed: int = 0
    additions: int = 0
    deletions: int = 0
    candidate_findings: int = 0
    published_findings: int = 0
    rejected_findings: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    duration_seconds: float = 0.0


class ReviewResult(BaseModel):
    """Final, verified review: what gets published."""

    summary: str
    findings: list[Finding] = Field(default_factory=list)
    rejected: list[RejectedFinding] = Field(default_factory=list)
    unanchored: list[Finding] = Field(default_factory=list)
    stats: ReviewStats = Field(default_factory=ReviewStats)

    def worst_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return min((f.severity for f in self.findings), key=lambda s: SEVERITY_RANK[s])
