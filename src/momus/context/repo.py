"""Read-only, sandboxed repository access for the agent.

Every method returns a string (results or a descriptive error) and never
raises: the output goes straight back to the model as a tool result, and the
model is expected to recover from its own mistakes (bad path, bad regex).

All paths are resolved and confined to the repository root, so a confused or
adversarial diff can't walk the tool out of the checkout.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_MAX_READ_LINES = 400
_MAX_RESULT_CHARS = 20_000
_MAX_SEARCH_HITS = 50
_MAX_TREE_ENTRIES = 400
_SUBPROCESS_TIMEOUT = 10.0
_SKIP_DIRS = {".git", ".hg", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache"}


def _clip(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[output clipped at {limit} characters]"


class RepoContext:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _resolve(self, relative: str) -> Path | None:
        try:
            candidate = (self.root / relative).resolve()
        except OSError:
            return None
        if candidate == self.root or candidate.is_relative_to(self.root):
            return candidate
        return None

    # -- tools exposed to the model -------------------------------------------------

    def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        target = self._resolve(path)
        if target is None:
            return f"error: path {path!r} is outside the repository"
        if not target.is_file():
            return f"error: {path!r} does not exist or is not a file"
        try:
            data = target.read_bytes()
        except OSError as exc:
            return f"error: could not read {path!r}: {exc}"
        if b"\x00" in data[:8192]:
            return f"error: {path!r} looks binary ({len(data)} bytes)"
        lines = data.decode("utf-8", errors="replace").splitlines()

        first = max(1, start_line or 1)
        last = min(len(lines), end_line or len(lines))
        if first > len(lines):
            return f"error: {path!r} has only {len(lines)} lines"
        window = lines[first - 1 : last]
        clipped = ""
        if len(window) > _MAX_READ_LINES:
            window = window[:_MAX_READ_LINES]
            clipped = (
                f"\n[stopped after {_MAX_READ_LINES} lines — "
                f"call read_file again with a start_line to continue]"
            )
        body = "\n".join(f"{first + i:>5} | {line}" for i, line in enumerate(window))
        header = f"{path} (lines {first}-{first + len(window) - 1} of {len(lines)})"
        return _clip(f"{header}\n{body}{clipped}")

    def search(self, pattern: str, glob: str | None = None) -> str:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"error: invalid regex {pattern!r}: {exc}"
        hits = self._search_ripgrep(pattern, glob)
        if hits is None:
            hits = self._search_python(compiled, glob)
        if not hits:
            return f"no matches for {pattern!r}" + (f" (glob {glob!r})" if glob else "")
        shown = hits[:_MAX_SEARCH_HITS]
        suffix = (
            f"\n[{len(hits) - len(shown)} more matches not shown — narrow the pattern]"
            if len(hits) > len(shown)
            else ""
        )
        return _clip("\n".join(shown) + suffix)

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if target is None:
            return f"error: path {path!r} is outside the repository"
        if not target.is_dir():
            return f"error: {path!r} is not a directory"
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        rows = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries]
        if not rows:
            return f"{path}: (empty directory)"
        return _clip("\n".join(rows[:_MAX_TREE_ENTRIES]))

    # -- pipeline helpers -----------------------------------------------------------

    def tree(self, max_entries: int = _MAX_TREE_ENTRIES) -> str:
        """Flat file listing used to prime the reviewer's mental map of the repo."""
        paths = self._git_tracked_files()
        if paths is None:
            paths = [
                str(p.relative_to(self.root))
                for p in sorted(self.root.rglob("*"))
                if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts)
            ]
        if len(paths) > max_entries:
            return "\n".join(paths[:max_entries]) + f"\n[{len(paths) - max_entries} more files]"
        return "\n".join(paths)

    # -- internals ------------------------------------------------------------------

    def _git_tracked_files(self) -> list[str] | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), "ls-files"],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return [line for line in proc.stdout.splitlines() if line]

    def _search_ripgrep(self, pattern: str, glob: str | None) -> list[str] | None:
        cmd = ["rg", "--no-heading", "--line-number", "--color", "never", "--max-columns", "300"]
        if glob:
            cmd += ["--glob", glob]
        cmd += ["--regexp", pattern, "."]
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode not in (0, 1):  # 1 = no matches
            return None
        return [line.removeprefix("./") for line in proc.stdout.splitlines() if line]

    def _search_python(self, compiled: re.Pattern[str], glob: str | None) -> list[str]:
        hits: list[str] = []
        for file in sorted(self.root.rglob(glob or "*")):
            if not file.is_file() or any(part in _SKIP_DIRS for part in file.parts):
                continue
            try:
                if file.stat().st_size > 1_000_000:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            rel = file.relative_to(self.root)
            for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if compiled.search(line):
                    hits.append(f"{rel}:{lineno}:{line.strip()[:300]}")
                    if len(hits) > _MAX_SEARCH_HITS * 2:
                        return hits
        return hits
