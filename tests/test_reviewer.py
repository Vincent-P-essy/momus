from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import SAMPLE_DIFF, FakeLLM, finding_payload, make_call, text_turn, tool_turn
from momus.agent import run_reviewer
from momus.agent.tools import SUBMIT_FINDINGS
from momus.config import MomusConfig
from momus.context import RepoContext
from momus.diff import AnchorIndex, parse_unified_diff, render_diff


def _run(llm: FakeLLM, repo: Path, cfg: MomusConfig) -> Any:
    files = parse_unified_diff(SAMPLE_DIFF)
    return run_reviewer(
        llm,
        RepoContext(repo),
        AnchorIndex(files),
        cfg,
        pr_title="Add encoding parameter",
        pr_body="Small change.",
        diff_text=render_diff(files),
        guidelines=None,
    )


def _submission(*findings: dict[str, Any], summary: str = "Looks focused.") -> Any:
    return tool_turn(
        make_call(SUBMIT_FINDINGS, call_id="submit_1", summary=summary, findings=list(findings))
    )


def test_happy_path_investigates_then_submits(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            tool_turn(make_call("read_file", path="src/app.py", start_line=None, end_line=None)),
            _submission(finding_payload()),
        ]
    )
    draft = _run(llm, sample_repo, cfg)

    assert draft.summary == "Looks focused."
    assert len(draft.findings) == 1
    assert draft.findings[0].line == 4
    assert draft.unanchored == []

    first, second = llm.requests
    assert first.tool_choice is None
    assert first.system.startswith("You are Momus")
    # The tool result of the read is in the second request's history.
    tool_results = [
        block
        for message in second.messages
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert "import os" in tool_results[0]["content"]
    # The rolling cache breakpoint sits on the newest block only.
    marked = [
        block
        for message in second.messages
        for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    ]
    assert len(marked) == 1


def test_parallel_tool_calls_answered_in_one_message(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            tool_turn(
                make_call("read_file", "c1", path="src/app.py", start_line=None, end_line=None),
                make_call("search", "c2", pattern="def load", glob=None),
            ),
            _submission(),
        ]
    )
    _run(llm, sample_repo, cfg)
    results_message = llm.requests[1].messages[-1]
    assert results_message["role"] == "user"
    ids = [block["tool_use_id"] for block in results_message["content"]]
    assert ids == ["c1", "c2"]


def test_bad_anchor_bounces_then_repairs(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            _submission(finding_payload(line=999)),
            _submission(finding_payload(line=4)),
        ]
    )
    draft = _run(llm, sample_repo, cfg)
    assert len(draft.findings) == 1
    assert draft.findings[0].line == 4

    repair_message = llm.requests[1].messages[-1]
    (block,) = repair_message["content"][:1]
    assert block["is_error"] is True
    assert "not a commentable diff line" in block["content"]


def test_repair_budget_exhausted_keeps_best_effort(sample_repo: Path, cfg: MomusConfig) -> None:
    bad = _submission(finding_payload(line=999), finding_payload(line=4, title="Good one"))
    llm = FakeLLM(script=[bad, bad, bad])
    draft = _run(llm, sample_repo, cfg)
    # After _MAX_REPAIRS bounces the submission is accepted as-is.
    assert [f.title for f in draft.findings] == ["Good one"]
    assert len(draft.unanchored) == 1


def test_last_iteration_forces_submission(sample_repo: Path) -> None:
    cfg = MomusConfig(max_iterations=1)
    llm = FakeLLM(script=[_submission(finding_payload())])
    draft = _run(llm, sample_repo, cfg)
    assert llm.requests[0].tool_choice == {"type": "tool", "name": SUBMIT_FINDINGS}
    assert len(draft.findings) == 1


def test_text_only_turn_gets_a_nudge(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(script=[text_turn("Let me think."), _submission()])
    _run(llm, sample_repo, cfg)
    nudge = llm.requests[1].messages[-1]
    assert "call submit_findings" in nudge["content"][0]["text"]


def test_duplicate_findings_keep_worst_severity(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            _submission(
                finding_payload(severity="minor", title="dup"),
                finding_payload(severity="blocker", title="dup"),
            )
        ]
    )
    draft = _run(llm, sample_repo, cfg)
    assert len(draft.findings) == 1
    assert draft.findings[0].severity.value == "blocker"


def test_max_findings_cap_is_noted(sample_repo: Path) -> None:
    cfg = MomusConfig(max_findings=1, max_iterations=6)
    llm = FakeLLM(
        script=[
            _submission(
                finding_payload(line=3, title="first", severity="blocker"),
                finding_payload(line=5, title="second", severity="nit", category="docs"),
            )
        ]
    )
    draft = _run(llm, sample_repo, cfg)
    assert len(draft.findings) == 1
    assert draft.findings[0].title == "first"
    assert "withheld by the max_findings cap" in draft.summary


def test_invalid_finding_shape_is_reported(sample_repo: Path, cfg: MomusConfig) -> None:
    llm = FakeLLM(
        script=[
            _submission({"path": "src/app.py", "title": ""}),
            _submission(finding_payload()),
        ]
    )
    draft = _run(llm, sample_repo, cfg)
    assert len(draft.findings) == 1
    bounce = llm.requests[1].messages[-1]["content"][0]
    assert "findings[0]" in bounce["content"]
