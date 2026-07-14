"""The adversarial verification pass.

Each candidate finding is handed to a fresh conversation whose only job is to
refute it against the actual code. Findings the verifier cannot refute are
upheld; everything else is rejected or revised. If verification cannot
conclude (iteration budget, malformed verdicts), the finding is REJECTED —
precision is the default, not the exception.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from momus.agent.reviewer import tool_result
from momus.agent.tools import (
    SUBMIT_VERDICT,
    context_tools,
    run_context_tool,
    submit_verdict_tool,
)
from momus.config import MomusConfig
from momus.context import RepoContext
from momus.llm import LLMClient, LLMRequest, with_rolling_cache_breakpoint
from momus.models import Finding, Verdict, Verification
from momus.prompts import VERIFIER_SYSTEM, verifier_user_prompt


def run_verifier(
    llm: LLMClient,
    ctx: RepoContext,
    cfg: MomusConfig,
    finding: Finding,
    file_diff_text: str,
) -> Verification:
    system = VERIFIER_SYSTEM.replace("{language}", cfg.language)
    user = verifier_user_prompt(finding.model_dump_json(indent=2), file_diff_text)
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    tools = [*context_tools(), submit_verdict_tool()]
    repaired = False

    for iteration in range(cfg.verifier_max_iterations):
        force = iteration >= cfg.verifier_max_iterations - 1
        turn = llm.complete(
            LLMRequest(
                model=cfg.effective_verifier_model,
                system=system,
                messages=with_rolling_cache_breakpoint(messages),
                tools=tools,
                tool_choice={"type": "tool", "name": SUBMIT_VERDICT} if force else None,
                max_tokens=cfg.max_tokens,
                effort=cfg.effort,
            )
        )
        messages.append({"role": "assistant", "content": turn.raw_content})

        verdict_call = next((c for c in turn.tool_calls if c.name == SUBMIT_VERDICT), None)
        if verdict_call is not None:
            try:
                return Verification.model_validate(verdict_call.input)
            except ValidationError as exc:
                if repaired or force:
                    break
                repaired = True
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            tool_result(
                                verdict_call.id,
                                f"Invalid verdict: {exc.errors()[0]['msg']}. "
                                "Call submit_verdict again.",
                                is_error=True,
                            )
                        ],
                    }
                )
                continue

        if turn.tool_calls:
            results = [tool_result(c.id, run_context_tool(ctx, c)) for c in turn.tool_calls]
            messages.append({"role": "user", "content": results})
            continue

        messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": "Deliver your verdict with submit_verdict."}],
            }
        )

    return Verification(
        verdict=Verdict.REJECT,
        rebuttal="verification did not conclude within its budget; withheld by default",
    )
