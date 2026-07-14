from __future__ import annotations

from pathlib import Path

from conftest import FakeLLM, make_call, make_finding, text_turn, tool_turn
from momus.agent import run_verifier
from momus.agent.tools import SUBMIT_VERDICT
from momus.config import MomusConfig
from momus.context import RepoContext
from momus.models import Confidence, Severity, Verdict


def _verdict_call(**overrides: object) -> object:
    payload: dict[str, object] = {
        "verdict": "uphold",
        "rebuttal": "Read the function; the leak is real.",
        "revised_severity": None,
        "revised_confidence": None,
        "revised_body": None,
    }
    payload.update(overrides)
    return make_call(SUBMIT_VERDICT, call_id="v1", **payload)


def test_uphold_after_investigation(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            tool_turn(make_call("read_file", path="src/app.py", start_line=None, end_line=None)),
            tool_turn(_verdict_call()),
        ]
    )
    verification = run_verifier(llm, RepoContext(sample_repo), cfg, make_finding(), "diff text")
    assert verification.verdict is Verdict.UPHOLD
    # The finding under scrutiny is embedded in the first user message.
    first_text = llm.requests[0].messages[0]["content"][0]["text"]
    assert "File handle is never closed" in first_text


def test_reject_carries_rebuttal(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(script=[tool_turn(_verdict_call(verdict="reject", rebuttal="covered by caller"))])
    verification = run_verifier(llm, RepoContext(sample_repo), cfg, make_finding(), "diff")
    assert verification.verdict is Verdict.REJECT
    assert verification.rebuttal == "covered by caller"


def test_revise_applies_corrections(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            tool_turn(
                _verdict_call(
                    verdict="revise",
                    revised_severity="minor",
                    revised_confidence="high",
                    revised_body="Real but only on the error path.",
                )
            )
        ]
    )
    verification = run_verifier(llm, RepoContext(sample_repo), cfg, make_finding(), "diff")
    revised = verification.apply_to(make_finding())
    assert revised.severity is Severity.MINOR
    assert revised.confidence is Confidence.HIGH
    assert revised.body == "Real but only on the error path."


def test_invalid_verdict_is_repaired_once(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            tool_turn(make_call(SUBMIT_VERDICT, call_id="bad", verdict="maybe", rebuttal="")),
            tool_turn(_verdict_call()),
        ]
    )
    verification = run_verifier(llm, RepoContext(sample_repo), cfg, make_finding(), "diff")
    assert verification.verdict is Verdict.UPHOLD
    bounce = llm.requests[1].messages[-1]["content"][0]
    assert bounce["is_error"] is True


def test_no_conclusion_defaults_to_reject(sample_repo: Path) -> None:
    cfg = MomusConfig(verifier_max_iterations=2)
    llm = FakeLLM(script=[text_turn("hmm"), text_turn("still thinking")])
    verification = run_verifier(llm, RepoContext(sample_repo), cfg, make_finding(), "diff")
    assert verification.verdict is Verdict.REJECT
    assert "did not conclude" in verification.rebuttal
    # The final iteration force-invokes the verdict tool.
    assert llm.requests[-1].tool_choice == {"type": "tool", "name": SUBMIT_VERDICT}


def test_verifier_uses_configured_model(sample_repo: Path) -> None:
    cfg = MomusConfig(verifier_model="claude-sonnet-5", verifier_max_iterations=3)
    llm = FakeLLM(script=[tool_turn(_verdict_call())])
    run_verifier(llm, RepoContext(sample_repo), cfg, make_finding(), "diff")
    assert llm.requests[0].model == "claude-sonnet-5"
