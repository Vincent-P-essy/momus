"""Parser for git-style unified diffs.

Produces per-line old/new numbering, which is what makes precise GitHub review
comment anchoring possible. Handles renames, adds/deletes, binary files, mode
changes, quoted paths and missing trailing newlines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from momus.errors import DiffError

LineKind = Literal["context", "add", "del"]
FileStatus = Literal["added", "modified", "deleted", "renamed"]

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass(slots=True)
class HunkLine:
    kind: LineKind
    old_lineno: int | None
    new_lineno: int | None
    content: str
    no_newline: bool = False


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str
    lines: list[HunkLine] = field(default_factory=list)


@dataclass(slots=True)
class FileDiff:
    old_path: str | None
    new_path: str | None
    status: FileStatus
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def display_path(self) -> str:
        path = self.new_path or self.old_path
        if path is None:
            raise DiffError("file diff has neither an old nor a new path")
        return path


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of paths with special characters."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        return path[1:-1].encode().decode("unicode_escape").encode("latin-1").decode("utf-8")
    return path


def _strip_prefix(path: str, prefix: str) -> str:
    return path[len(prefix) :] if path.startswith(prefix) else path


def _path_from_marker(line: str, marker: str) -> str | None:
    """Extract a path from a `--- a/...` / `+++ b/...` line. None for /dev/null."""
    raw = line[len(marker) :].rstrip("\n")
    # Some diff producers append a tab + metadata after the path.
    raw = raw.split("\t", 1)[0]
    raw = _unquote(raw)
    if raw == "/dev/null":
        return None
    return _strip_prefix(_strip_prefix(raw, "a/"), "b/")


def _paths_from_git_header(line: str) -> tuple[str | None, str | None]:
    """Best-effort path extraction from a `diff --git a/x b/y` line.

    Only used when no ---/+++ or rename lines are present (binary files,
    mode-only changes). Ambiguous for unquoted paths containing ` b/`, which
    git itself cannot represent unambiguously on this line either.
    """
    rest = line[len("diff --git ") :].rstrip("\n")
    if rest.startswith('"'):
        match = re.match(r'^("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*"|.*)$', rest)
        if match:
            return (
                _strip_prefix(_unquote(match.group(1)), "a/"),
                _strip_prefix(_unquote(match.group(2)), "b/"),
            )
    match = re.match(r"^a/(.*?) b/(.*)$", rest)
    if match:
        return match.group(1), match.group(2)
    return None, None


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.pos = 0
        self.files: list[FileDiff] = []

    def peek(self) -> str | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def advance(self) -> str:
        line = self.lines[self.pos]
        self.pos += 1
        return line

    def parse(self) -> list[FileDiff]:
        while (line := self.peek()) is not None:
            if line.startswith("diff --git "):
                self._parse_file()
            else:
                self.pos += 1  # preamble / commit message / stat noise
        return self.files

    def _parse_file(self) -> None:
        header = self.advance()
        old_path: str | None = None
        new_path: str | None = None
        status: FileStatus = "modified"
        is_binary = False
        renamed = False
        saw_marker_paths = False

        while (line := self.peek()) is not None and not line.startswith("diff --git "):
            if line.startswith("--- "):
                old_path = _path_from_marker(self.advance(), "--- ")
                saw_marker_paths = True
            elif line.startswith("+++ "):
                new_path = _path_from_marker(self.advance(), "+++ ")
                saw_marker_paths = True
            elif line.startswith("rename from "):
                old_path = _unquote(self.advance()[len("rename from ") :])
                renamed = True
            elif line.startswith("rename to "):
                new_path = _unquote(self.advance()[len("rename to ") :])
                renamed = True
            elif line.startswith("new file mode"):
                status = "added"
                self.pos += 1
            elif line.startswith("deleted file mode"):
                status = "deleted"
                self.pos += 1
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                is_binary = True
                self.pos += 1
            elif line.startswith("@@ "):
                break
            elif line.startswith(
                ("index ", "old mode", "new mode", "similarity index", "dissimilarity index")
            ):
                self.pos += 1
            else:
                # Unknown header noise (e.g. `\ No newline` after binary note).
                self.pos += 1

        if not saw_marker_paths and not renamed:
            old_path, new_path = _paths_from_git_header(header)
        if renamed:
            status = "renamed"
        if status == "added":
            old_path = None
        if status == "deleted":
            new_path = None

        file_diff = FileDiff(
            old_path=old_path, new_path=new_path, status=status, is_binary=is_binary
        )
        while (line := self.peek()) is not None and line.startswith("@@ "):
            file_diff.hunks.append(self._parse_hunk())
        if file_diff.old_path is None and file_diff.new_path is None:
            raise DiffError(f"could not determine paths for diff header: {header!r}")
        self.files.append(file_diff)

    def _parse_hunk(self) -> Hunk:
        header = self.advance()
        match = _HUNK_RE.match(header)
        if match is None:
            raise DiffError(f"malformed hunk header: {header!r}")
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        hunk = Hunk(old_start, old_count, new_start, new_count, match.group(5).strip())

        old_line, new_line = old_start, new_start
        old_left, new_left = old_count, new_count
        while old_left > 0 or new_left > 0:
            line = self.peek()
            if line is None:
                raise DiffError("diff ended in the middle of a hunk")
            self.pos += 1
            if line.startswith("\\"):  # "\ No newline at end of file"
                if hunk.lines:
                    hunk.lines[-1].no_newline = True
                continue
            if line.startswith("+"):
                hunk.lines.append(HunkLine("add", None, new_line, line[1:]))
                new_line += 1
                new_left -= 1
            elif line.startswith("-"):
                hunk.lines.append(HunkLine("del", old_line, None, line[1:]))
                old_line += 1
                old_left -= 1
            elif line.startswith(" ") or line == "":
                hunk.lines.append(HunkLine("context", old_line, new_line, line[1:]))
                old_line += 1
                new_line += 1
                old_left -= 1
                new_left -= 1
            else:
                raise DiffError(f"unexpected line inside hunk: {line!r}")
        # A trailing "\ No newline" can follow the last consumed line.
        if (line := self.peek()) is not None and line.startswith("\\"):
            self.pos += 1
            if hunk.lines:
                hunk.lines[-1].no_newline = True
        return hunk


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Parse a git-style unified diff into structured per-file changes."""
    return _Parser(text).parse()


def count_changes(files: list[FileDiff]) -> tuple[int, int]:
    """Return (additions, deletions) across all files."""
    additions = sum(1 for f in files for h in f.hunks for line in h.lines if line.kind == "add")
    deletions = sum(1 for f in files for h in f.hunks for line in h.lines if line.kind == "del")
    return additions, deletions
