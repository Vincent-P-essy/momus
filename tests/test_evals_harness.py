"""The eval harness is code too: keep its plumbing and its cases honest.

These tests are fully offline — they exercise case materialisation, diff
generation and scoring, never the model.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import make_finding
from momus.diff import AnchorIndex, parse_unified_diff
from momus.models import ReviewResult, ReviewStats

_RUN_PY = Path(__file__).resolve().parents[1] / "evals" / "run.py"
_spec = importlib.util.spec_from_file_location("momus_evals_run", _RUN_PY)
assert _spec is not None and _spec.loader is not None
evals_run = importlib.util.module_from_spec(_spec)
# Register before exec: dataclasses resolve string annotations via sys.modules.
sys.modules["momus_evals_run"] = evals_run
_spec.loader.exec_module(evals_run)

CASE_NAMES = sorted(p.name for p in evals_run.CASES_DIR.iterdir() if p.is_dir())


@pytest.fixture(autouse=True)
def _work_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evals_run, "WORK_DIR", tmp_path / "work")


def test_the_suite_has_planted_and_clean_cases() -> None:
    assert len(CASE_NAMES) >= 10
    clean = [name for name in CASE_NAMES if name.startswith("clean-")]
    assert len(clean) >= 3, "clean cases are what measure false positives"


@pytest.mark.parametrize("name", CASE_NAMES)
def test_case_is_well_formed(name: str) -> None:
    case = evals_run.CASES_DIR / name
    expected = evals_run.load_expected(case)
    repo, diff = evals_run.build_case_repo(case)

    files = parse_unified_diff(diff)
    assert files, f"{name}: the base->head change produced an empty diff"

    # Every planted finding must be anchorable, or the case can never be caught.
    anchors = AnchorIndex(files)
    for planted in expected:
        probe = make_finding(
            path=planted.path,
            line=planted.line,
            category="bug" if planted.category == "any" else planted.category,
        )
        assert anchors.resolve(probe) is not None, f"{name}: {planted.path}:{planted.line}"

    # The working tree the agent reads must be the head state.
    for head_file in (case / "head").rglob("*"):
        if head_file.is_file():
            rel = head_file.relative_to(case / "head")
            assert (repo / rel).read_text() == head_file.read_text(), f"{name}: {rel}"


def test_scoring_counts_hits_within_tolerance() -> None:
    expected = [evals_run.Expected(path="a.py", line=10, category="bug", note="planted")]
    result = ReviewResult(
        summary="s",
        findings=[make_finding(path="a.py", line=12)],
        stats=ReviewStats(),
    )
    score = evals_run.score_case("case", expected, result, seconds=1.0)
    assert (score.caught, score.false_positives, score.missed) == (1, 0, [])


def test_scoring_counts_misses_and_false_positives() -> None:
    expected = [evals_run.Expected(path="a.py", line=10, category="bug", note="planted")]
    result = ReviewResult(
        summary="s",
        findings=[make_finding(path="b.py", line=10, title="Unrelated")],
        stats=ReviewStats(),
    )
    score = evals_run.score_case("case", expected, result, seconds=1.0)
    assert (score.caught, score.false_positives) == (0, 1)
    assert score.missed and "planted" in score.missed[0]
    assert score.spurious and "Unrelated" in score.spurious[0]


def test_category_must_match_unless_any() -> None:
    expected = evals_run.Expected(path="a.py", line=10, category="performance", note="n")
    assert not evals_run.matches(expected, make_finding(path="a.py", line=10))
    loose = evals_run.Expected(path="a.py", line=10, category="any", note="n")
    assert evals_run.matches(loose, make_finding(path="a.py", line=10))


def test_report_renders_precision_and_recall() -> None:
    from momus.config import MomusConfig

    scores = [
        evals_run.CaseScore(
            name="demo",
            planted=2,
            caught=1,
            false_positives=1,
            rejected=3,
            cost_usd=0.5,
            seconds=12.0,
        )
    ]
    report = evals_run.render_report(scores, MomusConfig())
    assert "precision 0.50" in report
    assert "recall 0.50" in report
    assert "| demo | 2 | 1/2 | 1 | 3 | $0.50 | 12s |" in report
