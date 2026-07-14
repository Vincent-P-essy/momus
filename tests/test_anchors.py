from __future__ import annotations

from conftest import SAMPLE_DIFF, make_finding
from momus.diff import AnchorIndex, parse_unified_diff
from momus.models import Side


def _index() -> AnchorIndex:
    return AnchorIndex(parse_unified_diff(SAMPLE_DIFF))


def test_exact_add_line_resolves() -> None:
    resolved = _index().resolve(make_finding(line=4))
    assert resolved is not None
    assert (resolved.side, resolved.line) == (Side.RIGHT, 4)


def test_context_line_resolves_on_right() -> None:
    resolved = _index().resolve(make_finding(line=8))
    assert resolved is not None
    assert resolved.line == 8


def test_snaps_to_nearest_within_window() -> None:
    # R9 does not exist; nearest commentable RIGHT line is R8 (context).
    resolved = _index().resolve(make_finding(line=9))
    assert resolved is not None
    assert resolved.line == 8


def test_far_line_is_unanchorable() -> None:
    assert _index().resolve(make_finding(line=50)) is None


def test_unknown_file_is_unanchorable() -> None:
    assert _index().resolve(make_finding(path="src/other.py")) is None


def test_left_side_deletion_resolves() -> None:
    resolved = _index().resolve(make_finding(side="LEFT", line=3))
    assert resolved is not None
    assert (resolved.side, resolved.line) == (Side.LEFT, 3)


def test_side_flip_when_only_other_side_matches() -> None:
    diff = """\
diff --git a/f.py b/f.py
--- a/f.py
+++ b/f.py
@@ -6,1 +5,0 @@
-obsolete = True
"""
    index = AnchorIndex(parse_unified_diff(diff))
    resolved = index.resolve(make_finding(path="f.py", side="RIGHT", line=6))
    assert resolved is not None
    assert (resolved.side, resolved.line) == (Side.LEFT, 6)


def test_valid_range_is_kept() -> None:
    resolved = _index().resolve(make_finding(line=5, start_line=3))
    assert resolved is not None
    assert resolved.start_line == 3


def test_invalid_range_falls_back_to_single_line() -> None:
    resolved = _index().resolve(make_finding(line=4, start_line=4))
    assert resolved is not None
    assert resolved.start_line is None
