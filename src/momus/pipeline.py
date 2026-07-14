"""Orchestration: diff in, verified ReviewResult out.

Stage order: parse -> filter -> reviewer (coverage) -> verifier (precision)
-> threshold filter -> stats. Publishing is the caller's job so the same
pipeline serves the GitHub Action, the CLI and the eval harness.
"""

from __future__ import annotations

import fnmatch
import subprocess
import time
from pathlib import Path

from momus.agent import run_reviewer, run_verifier
from momus.config import MomusConfig
from momus.context import RepoContext
from momus.diff import (
    AnchorIndex,
    FileDiff,
    count_changes,
    parse_unified_diff,
    render_diff,
    render_file_diff,
)
from momus.errors import BudgetExceededError, ConfigError, DiffError, MomusError
from momus.github import PRInfo
from momus.llm import AnthropicLLM, LLMClient, UsageTally
from momus.models import (
    SEVERITY_RANK,
    Finding,
    RejectedFinding,
    ReviewResult,
    ReviewStats,
    Verdict,
)


def local_diff(
    repo_root: Path, base: str | None, include_untracked: bool = False
) -> tuple[PRInfo, str]:
    """Diff the working tree against HEAD, or against `base...HEAD`.

    With `include_untracked`, brand-new files appear in the diff as additions:
    they are registered with `git add --intent-to-add` for the duration of the
    diff and the index is reset afterwards.
    """
    target = f"{base}...HEAD" if base and base != "HEAD" else "HEAD"
    if include_untracked:
        _run_git(repo_root, "add", "--intent-to-add", "--all")
    command = ["git", "-C", str(repo_root), "diff", "--no-color", "--no-ext-diff", target]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiffError(f"could not run git diff: {exc}") from exc
    if proc.returncode != 0:
        raise DiffError(f"git diff failed: {proc.stderr.strip() or proc.stdout.strip()}")
    if include_untracked:
        _run_git(repo_root, "reset", "--quiet")
    pr = PRInfo(
        number=0,
        title=f"Local changes ({target})",
        body="",
        head_sha="",
        base_ref=base or "HEAD",
        head_ref="worktree",
        author="",
        draft=False,
    )
    return pr, proc.stdout


def _run_git(repo_root: Path, *args: str) -> None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiffError(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        raise DiffError(f"git {args[0]} failed: {proc.stderr.strip()}")


def run_review(
    repo_root: Path,
    pr: PRInfo,
    diff_text: str,
    cfg: MomusConfig,
    llm: LLMClient | None = None,
    tally: UsageTally | None = None,
) -> ReviewResult:
    started = time.monotonic()
    files = [
        f for f in parse_unified_diff(diff_text) if not _is_ignored(f.display_path, cfg.ignore)
    ]
    reviewable = [f for f in files if not f.is_binary and f.hunks]
    if not reviewable:
        return ReviewResult(
            summary="No reviewable changes (empty, binary-only, or fully ignored diff).",
            stats=ReviewStats(duration_seconds=time.monotonic() - started),
        )

    tally = tally or UsageTally()
    if llm is None:
        llm = _build_llm(tally, cfg)
    ctx = RepoContext(repo_root)
    anchors = AnchorIndex(reviewable)
    rendered = render_diff(reviewable, cfg.max_diff_chars)
    guidelines = _load_guidelines(repo_root, cfg)

    draft = run_reviewer(llm, ctx, anchors, cfg, pr.title, pr.body, rendered, guidelines)

    kept: list[Finding] = []
    rejected: list[RejectedFinding] = []
    if cfg.verify and draft.findings:
        kept, rejected = _verify_stage(llm, ctx, cfg, draft.findings, reviewable)
    else:
        kept = sorted(draft.findings, key=Finding.sort_key)

    threshold = SEVERITY_RANK[cfg.min_severity]
    below = [f for f in kept if SEVERITY_RANK[f.severity] > threshold]
    kept = [f for f in kept if SEVERITY_RANK[f.severity] <= threshold]
    threshold_note = f"below the min_severity threshold ({cfg.min_severity.value})"
    rejected += [RejectedFinding(finding=f, rebuttal=threshold_note) for f in below]

    additions, deletions = count_changes(reviewable)
    totals = tally.totals()
    stats = ReviewStats(
        files_reviewed=len(reviewable),
        additions=additions,
        deletions=deletions,
        candidate_findings=len(draft.findings) + len(draft.unanchored),
        published_findings=len(kept),
        rejected_findings=len(rejected),
        model_calls=tally.calls,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        cache_write_tokens=totals.cache_write_tokens,
        cost_usd=tally.cost_usd(),
        duration_seconds=time.monotonic() - started,
    )
    return ReviewResult(
        summary=draft.summary,
        findings=kept,
        rejected=rejected,
        unanchored=draft.unanchored,
        stats=stats,
    )


def _verify_stage(
    llm: LLMClient,
    ctx: RepoContext,
    cfg: MomusConfig,
    findings: list[Finding],
    files: list[FileDiff],
) -> tuple[list[Finding], list[RejectedFinding]]:
    diff_by_path = {f.display_path: render_file_diff(f) for f in files}
    kept: list[Finding] = []
    rejected: list[RejectedFinding] = []
    queue = sorted(findings, key=Finding.sort_key)  # verify the worst first
    for index, finding in enumerate(queue):
        file_diff = diff_by_path.get(finding.path, "(diff unavailable)")
        try:
            verification = run_verifier(llm, ctx, cfg, finding, file_diff)
        except BudgetExceededError:
            rejected += [
                RejectedFinding(
                    finding=remaining,
                    rebuttal="not verified: the review budget ran out; withheld by default",
                )
                for remaining in queue[index:]
            ]
            break
        if verification.verdict is Verdict.REJECT:
            rejected.append(RejectedFinding(finding=finding, rebuttal=verification.rebuttal))
        else:
            kept.append(verification.apply_to(finding))
    return kept, rejected


def _build_llm(tally: UsageTally, cfg: MomusConfig) -> LLMClient:
    try:
        return AnthropicLLM(tally=tally, budget_usd=cfg.budget_usd)
    except Exception as exc:
        raise MomusError(
            f"could not initialise the Anthropic client: {exc}\n"
            "Set ANTHROPIC_API_KEY (or authenticate with `ant auth login`)."
        ) from exc


def _is_ignored(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        stripped = pattern.removeprefix("**/")
        if stripped != pattern and fnmatch.fnmatch(path, stripped):
            return True
    return False


def _load_guidelines(repo_root: Path, cfg: MomusConfig) -> str | None:
    if cfg.guidelines is None:
        return None
    path = repo_root / cfg.guidelines
    if not path.is_file():
        raise ConfigError(f"guidelines file not found: {cfg.guidelines}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > 20_000:
        text = text[:20_000] + "\n[guidelines truncated]"
    return text
