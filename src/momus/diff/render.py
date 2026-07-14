"""Render parsed diffs for model consumption.

Every line carries an explicit `R<n>` (new file) or `L<n>` (old file) number.
The model anchors findings to these numbers, which map 1:1 onto the GitHub
review API's `line` + `side` parameters — no fragile position arithmetic.
"""

from __future__ import annotations

from momus.diff.parser import FileDiff, Hunk

_TRUNCATION_NOTICE = (
    "\n[diff truncated here to fit the review budget — use read_file for anything beyond]\n"
)


def _render_hunk(hunk: Hunk) -> str:
    out: list[str] = [f"@@ {hunk.section} @@" if hunk.section else "@@"]
    for line in hunk.lines:
        if line.kind == "add":
            out.append(f"+ R{line.new_lineno} | {line.content}")
        elif line.kind == "del":
            out.append(f"- L{line.old_lineno} | {line.content}")
        else:
            out.append(f"  R{line.new_lineno} | {line.content}")
        if line.no_newline:
            out.append("  (no newline at end of file)")
    return "\n".join(out)


def render_file_diff(file: FileDiff) -> str:
    """Render one file's diff with explicit line anchors."""
    if file.status == "renamed" and file.old_path != file.new_path:
        title = f"### {file.old_path} -> {file.new_path} (renamed)"
    else:
        title = f"### {file.display_path} ({file.status})"
    if file.is_binary:
        return f"{title}\n(binary file — not reviewable)"
    if not file.hunks:
        return f"{title}\n(no content changes)"
    return "\n\n".join([title, *(_render_hunk(h) for h in file.hunks)])


def render_diff(files: list[FileDiff], max_chars: int = 240_000) -> str:
    """Render the whole diff, truncating whole files past the character budget."""
    parts: list[str] = []
    used = 0
    for index, file in enumerate(files):
        rendered = render_file_diff(file)
        if used + len(rendered) > max_chars and parts:
            skipped = len(files) - index
            parts.append(
                _TRUNCATION_NOTICE
                + f"[{skipped} more changed file(s) omitted: "
                + ", ".join(f.display_path for f in files[index:])
                + "]"
            )
            break
        parts.append(rendered)
        used += len(rendered)
    return "\n\n".join(parts)
