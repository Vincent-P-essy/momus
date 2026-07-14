from __future__ import annotations

import pytest

from conftest import SAMPLE_DIFF
from momus.diff import count_changes, parse_unified_diff, render_diff, render_file_diff
from momus.errors import DiffError


def test_parse_modified_file() -> None:
    files = parse_unified_diff(SAMPLE_DIFF)
    assert len(files) == 1
    file = files[0]
    assert file.status == "modified"
    assert file.old_path == "src/app.py"
    assert file.new_path == "src/app.py"
    assert len(file.hunks) == 1

    hunk = file.hunks[0]
    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (1, 7, 1, 8)
    kinds = [line.kind for line in hunk.lines]
    assert kinds == [
        "context",
        "context",
        "del",
        "del",
        "add",
        "add",
        "add",
        "context",
        "context",
        "context",
    ]
    adds = [line for line in hunk.lines if line.kind == "add"]
    assert [line.new_lineno for line in adds] == [3, 4, 5]
    assert all(line.old_lineno is None for line in adds)
    dels = [line for line in hunk.lines if line.kind == "del"]
    assert [line.old_lineno for line in dels] == [3, 4]
    last = hunk.lines[-1]
    assert (last.old_lineno, last.new_lineno) == (7, 8)


def test_count_changes() -> None:
    files = parse_unified_diff(SAMPLE_DIFF)
    assert count_changes(files) == (3, 2)


def test_added_and_deleted_files() -> None:
    diff = """\
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+print("hello")
+print("world")
diff --git a/old.py b/old.py
deleted file mode 100644
index e69de29..0000000
--- a/old.py
+++ /dev/null
@@ -1,1 +0,0 @@
-print("bye")
"""
    added, deleted = parse_unified_diff(diff)
    assert added.status == "added"
    assert added.old_path is None
    assert added.new_path == "new.py"
    assert [line.new_lineno for line in added.hunks[0].lines] == [1, 2]

    assert deleted.status == "deleted"
    assert deleted.new_path is None
    assert deleted.old_path == "old.py"
    assert deleted.hunks[0].lines[0].old_lineno == 1


def test_rename_without_content_change() -> None:
    diff = """\
diff --git a/lib/a.py b/lib/b.py
similarity index 100%
rename from lib/a.py
rename to lib/b.py
"""
    (file,) = parse_unified_diff(diff)
    assert file.status == "renamed"
    assert file.old_path == "lib/a.py"
    assert file.new_path == "lib/b.py"
    assert file.hunks == []


def test_rename_with_edits() -> None:
    diff = """\
diff --git a/lib/a.py b/lib/b.py
similarity index 90%
rename from lib/a.py
rename to lib/b.py
index 1111111..2222222 100644
--- a/lib/a.py
+++ b/lib/b.py
@@ -1,2 +1,2 @@
 x = 1
-y = 2
+y = 3
"""
    (file,) = parse_unified_diff(diff)
    assert file.status == "renamed"
    assert file.display_path == "lib/b.py"
    assert len(file.hunks) == 1


def test_binary_file() -> None:
    diff = """\
diff --git a/logo.png b/logo.png
index 1111111..2222222 100644
Binary files a/logo.png and b/logo.png differ
"""
    (file,) = parse_unified_diff(diff)
    assert file.is_binary
    assert file.display_path == "logo.png"
    assert "not reviewable" in render_file_diff(file)


def test_quoted_unicode_path() -> None:
    diff = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        "index 1111111..2222222 100644\n"
        '--- "a/caf\\303\\251.py"\n'
        '+++ "b/caf\\303\\251.py"\n'
        "@@ -1,1 +1,1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    (file,) = parse_unified_diff(diff)
    assert file.display_path == "café.py"


def test_path_with_spaces() -> None:
    diff = """\
diff --git a/my file.py b/my file.py
index 1111111..2222222 100644
--- a/my file.py
+++ b/my file.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
"""
    (file,) = parse_unified_diff(diff)
    assert file.display_path == "my file.py"


def test_no_newline_marker() -> None:
    diff = """\
diff --git a/f.txt b/f.txt
index 1111111..2222222 100644
--- a/f.txt
+++ b/f.txt
@@ -1,1 +1,1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""
    (file,) = parse_unified_diff(diff)
    lines = file.hunks[0].lines
    assert lines[0].no_newline
    assert lines[1].no_newline


def test_preamble_is_ignored() -> None:
    diff = "commit deadbeef\nAuthor: someone\n\n" + SAMPLE_DIFF
    assert len(parse_unified_diff(diff)) == 1


def test_empty_diff() -> None:
    assert parse_unified_diff("") == []


def test_truncated_hunk_raises() -> None:
    diff = """\
diff --git a/f.py b/f.py
--- a/f.py
+++ b/f.py
@@ -1,3 +1,3 @@
 line one
"""
    with pytest.raises(DiffError):
        parse_unified_diff(diff)


def test_render_carries_anchors() -> None:
    files = parse_unified_diff(SAMPLE_DIFF)
    rendered = render_file_diff(files[0])
    assert "+ R4 | " in rendered
    assert "- L3 | " in rendered
    assert "  R8 | " in rendered


def test_render_diff_truncates_whole_files() -> None:
    files = parse_unified_diff(SAMPLE_DIFF + SAMPLE_DIFF.replace("src/app.py", "src/b.py"))
    rendered = render_diff(files, max_chars=len(render_file_diff(files[0])) + 10)
    assert "src/app.py" in rendered
    assert "omitted" in rendered
    assert "src/b.py" in rendered  # named in the omission notice
