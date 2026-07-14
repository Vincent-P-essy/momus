"""Validate and repair finding anchors against the actual diff.

GitHub rejects review comments on lines that are not part of the PR's hunks.
Rather than letting the whole review 422, we validate every anchor up front:
exact match, then snap to the nearest commentable line within a small window,
otherwise report the finding as unanchorable (it still appears in the summary).
"""

from __future__ import annotations

from momus.diff.parser import FileDiff
from momus.models import Finding, Side

_SNAP_WINDOW = 3


class AnchorIndex:
    """Commentable (side, line) pairs per file, derived from parsed hunks."""

    def __init__(self, files: list[FileDiff]) -> None:
        self._lines: dict[str, dict[Side, set[int]]] = {}
        for file in files:
            if file.is_binary:
                continue
            slots = self._lines.setdefault(file.display_path, {Side.LEFT: set(), Side.RIGHT: set()})
            for hunk in file.hunks:
                for line in hunk.lines:
                    if line.kind == "add" and line.new_lineno is not None:
                        slots[Side.RIGHT].add(line.new_lineno)
                    elif line.kind == "del" and line.old_lineno is not None:
                        slots[Side.LEFT].add(line.old_lineno)
                    elif line.kind == "context" and line.new_lineno is not None:
                        slots[Side.RIGHT].add(line.new_lineno)

    def known_paths(self) -> set[str]:
        return set(self._lines)

    def resolve(self, finding: Finding) -> Finding | None:
        """Return the finding with a valid anchor (possibly snapped), or None."""
        slots = self._lines.get(finding.path)
        if slots is None:
            return None
        candidates = slots[finding.side]
        line = self._snap(finding.line, candidates)
        if line is None:
            # Deletions are often reported on the wrong side; try the other one.
            other = Side.LEFT if finding.side is Side.RIGHT else Side.RIGHT
            line = self._snap(finding.line, slots[other])
            if line is None:
                return None
            return finding.model_copy(update={"side": other, "line": line, "start_line": None})

        start = finding.start_line
        if start is not None and (start >= line or start not in candidates):
            start = None  # invalid range: fall back to a single-line comment
        return finding.model_copy(update={"line": line, "start_line": start})

    @staticmethod
    def _snap(line: int, candidates: set[int]) -> int | None:
        if line in candidates:
            return line
        near = [c for c in candidates if abs(c - line) <= _SNAP_WINDOW]
        if not near:
            return None
        return min(near, key=lambda c: (abs(c - line), c))
