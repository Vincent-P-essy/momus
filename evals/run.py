#!/usr/bin/env python3
"""Momus eval harness — measures the reviewer against planted, labelled bugs.

Each case under evals/cases/<name>/ contains:
    base/           repository state before the change
    head/           repository state the "PR" proposes
    expected.toml   planted findings ([[expected]] path/line/category/note);
                    an empty file marks a clean case (any finding is a FP)

For every case the runner materialises a real git repository from base/,
commits it, applies head/, takes the actual `git diff`, and runs the full
momus pipeline (reviewer + verifier) against the head checkout with a live
model. A planted finding counts as caught when a published finding lands on
the same file within 3 lines with the expected category ("any" matches all).
Published findings that match nothing planted count as false positives.

Usage:
    uv run python evals/run.py [--cases NAME ...] [--model ID]
                               [--effort LEVEL] [--budget USD] [--no-verify]

Requires ANTHROPIC_API_KEY. Writes evals/report.md and evals/report.json.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from momus.config import MomusConfig
from momus.github import PRInfo
from momus.models import Finding, ReviewResult
from momus.pipeline import run_review
from momus.prompts import PROMPT_VERSION

EVALS_DIR = Path(__file__).resolve().parent
CASES_DIR = EVALS_DIR / "cases"
WORK_DIR = EVALS_DIR / ".work"
LINE_TOLERANCE = 3


@dataclass
class Expected:
    path: str
    line: int
    category: str
    note: str


@dataclass
class CaseScore:
    name: str
    planted: int
    caught: int
    false_positives: int
    rejected: int
    cost_usd: float | None
    seconds: float
    missed: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)
    error: str | None = None


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout


def _copy_tree(src: Path, dst: Path) -> None:
    for item in src.rglob("*"):
        if item.is_file():
            target = dst / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def build_case_repo(case: Path) -> tuple[Path, str]:
    """Base tree committed, head tree staged; returns (repo, real git diff)."""
    repo = WORK_DIR / case.name
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    _copy_tree(case / "base", repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "evals@momus.local")
    _git(repo, "config", "user.name", "Momus Evals")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base state")

    for tracked in _git(repo, "ls-files").splitlines():
        (repo / tracked).unlink()
    _copy_tree(case / "head", repo)
    _git(repo, "add", "-A")
    diff = _git(repo, "diff", "--cached", "--no-color")
    return repo, diff


def load_expected(case: Path) -> list[Expected]:
    data = tomllib.loads((case / "expected.toml").read_text(encoding="utf-8"))
    return [Expected(**item) for item in data.get("expected", [])]


def matches(expected: Expected, finding: Finding) -> bool:
    if finding.path != expected.path:
        return False
    if abs(finding.line - expected.line) > LINE_TOLERANCE:
        return False
    return expected.category in ("any", finding.category.value)


def score_case(
    name: str, expected: list[Expected], result: ReviewResult, seconds: float
) -> CaseScore:
    findings = list(result.findings)
    caught: list[Expected] = []
    missed: list[Expected] = []
    for planted in expected:
        hit = next((f for f in findings if matches(planted, f)), None)
        if hit is None:
            missed.append(planted)
        else:
            findings.remove(hit)
            caught.append(planted)
    return CaseScore(
        name=name,
        planted=len(expected),
        caught=len(caught),
        false_positives=len(findings),
        rejected=len(result.rejected),
        cost_usd=result.stats.cost_usd,
        seconds=seconds,
        missed=[f"{m.path}:{m.line} — {m.note}" for m in missed],
        spurious=[f"{f.path}:{f.line} — {f.title}" for f in findings],
    )


def run_case(case: Path, cfg: MomusConfig) -> CaseScore:
    expected = load_expected(case)
    repo, diff = build_case_repo(case)
    pr = PRInfo(
        number=0,
        title=f"Eval case: {case.name}",
        body="",
        head_sha="",
        base_ref="main",
        head_ref="eval",
        author="evals",
        draft=False,
    )
    started = time.monotonic()
    result = run_review(repo, pr, diff, cfg)
    return score_case(case.name, expected, result, time.monotonic() - started)


def render_report(scores: list[CaseScore], cfg: MomusConfig) -> str:
    tp = sum(s.caught for s in scores)
    fp = sum(s.false_positives for s in scores)
    fn = sum(s.planted - s.caught for s in scores)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    total_cost = sum(s.cost_usd or 0.0 for s in scores)

    lines = [
        "# Momus eval report",
        "",
        f"- date: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- model: `{cfg.model}` (verifier: `{cfg.effective_verifier_model}`)",
        f"- effort: {cfg.effort} · verify: {cfg.verify} · prompt: {PROMPT_VERSION}",
        f"- cases: {len(scores)} · planted bugs: {tp + fn}",
        "",
        f"**precision {precision:.2f} · recall {recall:.2f} · F1 {f1:.2f} · "
        f"total cost ${total_cost:.2f}**",
        "",
        "| case | planted | caught | false+ | rejected by verifier | cost | time |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        cost = f"${s.cost_usd:.2f}" if s.cost_usd is not None else "n/a"
        status = "error" if s.error else f"{s.caught}/{s.planted}"
        lines.append(
            f"| {s.name} | {s.planted} | {status} | {s.false_positives} "
            f"| {s.rejected} | {cost} | {s.seconds:.0f}s |"
        )
    for s in scores:
        if s.error:
            lines += ["", f"## {s.name} — ERROR", "", f"```\n{s.error}\n```"]
            continue
        if s.missed or s.spurious:
            lines += ["", f"## {s.name}"]
            for miss in s.missed:
                lines.append(f"- missed: {miss}")
            for extra in s.spurious:
                lines.append(f"- false positive: {extra}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="*", help="case names (default: all)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--verifier-model", default=None)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--budget", type=float, default=1.0, help="USD budget per case")
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()

    cases = sorted(p for p in CASES_DIR.iterdir() if p.is_dir())
    if args.cases:
        wanted = set(args.cases)
        cases = [c for c in cases if c.name in wanted]
        unknown = wanted - {c.name for c in cases}
        if unknown:
            parser.error(f"unknown case(s): {', '.join(sorted(unknown))}")

    cfg = MomusConfig(
        model=args.model or MomusConfig().model,
        verifier_model=args.verifier_model,
        effort=args.effort or "high",
        verify=not args.no_verify,
        budget_usd=args.budget,
        cost_footer=False,
    )

    scores: list[CaseScore] = []
    for case in cases:
        print(f"[momus-evals] {case.name} ...", flush=True)
        try:
            score = run_case(case, cfg)
        except Exception as exc:  # keep going: one broken case must not kill the run
            score = CaseScore(
                name=case.name,
                planted=len(load_expected(case)),
                caught=0,
                false_positives=0,
                rejected=0,
                cost_usd=None,
                seconds=0.0,
                error=f"{type(exc).__name__}: {exc}",
            )
        scores.append(score)
        caught = f"{score.caught}/{score.planted}"
        print(
            f"[momus-evals]   caught {caught}, {score.false_positives} false positive(s), "
            f"{score.rejected} rejected, {score.seconds:.0f}s",
            flush=True,
        )

    report = render_report(scores, cfg)
    (EVALS_DIR / "report.md").write_text(report, encoding="utf-8")
    (EVALS_DIR / "report.json").write_text(
        json.dumps([s.__dict__ for s in scores], indent=2), encoding="utf-8"
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
