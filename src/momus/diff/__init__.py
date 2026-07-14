"""Unified-diff parsing, rendering for model consumption, and comment anchoring."""

from momus.diff.anchors import AnchorIndex
from momus.diff.parser import FileDiff, Hunk, HunkLine, count_changes, parse_unified_diff
from momus.diff.render import render_diff, render_file_diff

__all__ = [
    "AnchorIndex",
    "FileDiff",
    "Hunk",
    "HunkLine",
    "count_changes",
    "parse_unified_diff",
    "render_diff",
    "render_file_diff",
]
