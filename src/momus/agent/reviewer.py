"""The reviewer loop: investigate the diff with tools, then submit findings.

The loop is deliberately unforgiving about output quality: submissions are
validated against the domain model and the diff's real anchors, and invalid
submissions bounce back to the model as tool errors (bounded number of
repairs). On the final iteration the submission tool is force-invoked so a
run always terminates with a structured result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from momus.agent.tools import (
    SUBMIT_FINDINGS,
    context_tools,
    run_context_tool,
    submit_findings_tool,
)
from momus.config import MomusConfig
from momus.context import RepoContext
from momus.diff import AnchorIndex
from momus.errors import AgentError
from momus.llm import LLMClient, LLMRequest, with_rolling_cache_breakpoint
from momus.models import SEVERITY_RANK, Finding
from momus.prompts import REVIEWER_SYSTEM, reviewer_user_prompt

_MAX_REPAIRS = 2


@dataclass(slots=True)
class ReviewDraft:
    summary: str
    findings: list[Finding] = field(default_factory=list)
    unanchored: list[Finding] = field(default_factory=list)


def tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        result["is_error"] = True
    return result


def run_reviewer(
    llm: LLMClient,
    ctx: RepoContext,
    anchors: AnchorIndex,
    cfg: MomusConfig,
    pr_title: str,
    pr_body: str,
    diff_text: str,
    guidelines: str | None,
) -> ReviewDraft:
    system = REVIEWER_SYSTEM.replace("{language}", cfg.language)
    user = reviewer_user_prompt(pr_title, pr_body, ctx.tree(), guidelines, diff_text)
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    tools = [*context_tools(), submit_findings_tool()]
    repairs = 0

    for iteration in range(cfg.max_iterations):
        force = iteration >= cfg.max_iterations - 1
        turn = llm.complete(
            LLMRequest(
                model=cfg.model,
                system=system,
                messages=with_rolling_cache_breakpoint(messages),
                tools=tools,
                tool_choice={"type": "tool", "name": SUBMIT_FINDINGS} if force else None,
                max_tokens=cfg.max_tokens,
                effort=cfg.effort,
            )
        )
        messages.append({"role": "assistant", "content": turn.raw_content})

        submission = next((c for c in turn.tool_calls if c.name == SUBMIT_FINDINGS), None)
        if submission is not None:
            draft, problems = parse_submission(submission.input, anchors, cfg.max_findings)
            if not problems or repairs >= _MAX_REPAIRS or force:
                return draft
            repairs += 1
            results = [
                tool_result(
                    submission.id,
                    "The submission has problems:\n- "
                    + "\n- ".join(problems)
                    + "\nFix them and call submit_findings again with the full review.",
                    is_error=True,
                )
            ]
            results += [
                tool_result(c.id, "ignored: resolve the submission problems first")
                for c in turn.tool_calls
                if c is not submission
            ]
            messages.append({"role": "user", "content": results})
            continue

        if turn.tool_calls:
            results = [tool_result(c.id, run_context_tool(ctx, c)) for c in turn.tool_calls]
            messages.append({"role": "user", "content": results})
            continue

        # Text-only turn: remind the model how to finish.
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "When your investigation is complete, call submit_findings.",
                    }
                ],
            }
        )

    raise AgentError("the reviewer did not deliver a submission within its iteration budget")


def parse_submission(
    payload: dict[str, Any],
    anchors: AnchorIndex,
    max_findings: int,
) -> tuple[ReviewDraft, list[str]]:
    """Validate a submission; always returns a best-effort draft plus problems."""
    problems: list[str] = []
    summary = str(payload.get("summary") or "").strip() or "Review completed."
    raw = payload.get("findings")
    if not isinstance(raw, list):
        return ReviewDraft(summary=summary), ["`findings` must be a list"]

    anchored: dict[tuple[str, str, int, str], Finding] = {}
    unanchored: list[Finding] = []
    for index, item in enumerate(raw):
        try:
            finding = Finding.model_validate(item)
        except ValidationError as exc:
            problems.append(f"findings[{index}]: {_short_validation_error(exc)}")
            continue
        resolved = anchors.resolve(finding)
        if resolved is None:
            unanchored.append(finding)
            problems.append(
                f"findings[{index}] targets {finding.path}:{finding.side.value[0]}"
                f"{finding.line}, which is not a commentable diff line — re-anchor it "
                "to an R/L number shown in the diff"
            )
            continue
        key = (resolved.path, resolved.side.value, resolved.line, resolved.category.value)
        existing = anchored.get(key)
        if existing is None or SEVERITY_RANK[resolved.severity] < SEVERITY_RANK[existing.severity]:
            anchored[key] = resolved

    findings = sorted(anchored.values(), key=Finding.sort_key)
    if len(findings) > max_findings:
        withheld = len(findings) - max_findings
        findings = findings[:max_findings]
        summary += f"\n\n({withheld} lower-severity finding(s) withheld by the max_findings cap.)"
    return ReviewDraft(summary=summary, findings=findings, unanchored=unanchored), problems


def _short_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        location = ".".join(str(piece) for piece in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
