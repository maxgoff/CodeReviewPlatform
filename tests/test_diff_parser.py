from reviewbot.diff_parser import chunk_files, parse_diff, render_for_prompt, should_skip
from tests.fakes import AWS_EXAMPLE_KEY


def test_parses_files_and_statuses(sample_diff):
    files = {f.path: f for f in parse_diff(sample_diff)}
    assert set(files) == {"README.md", "app/users.py", "package-lock.json"}
    assert files["app/users.py"].status == "modified"


def test_new_file_line_numbers_match_source(sample_diff):
    users = next(f for f in parse_diff(sample_diff) if f.path == "app/users.py")
    assert users.line_text(10) == f'AWS_ACCESS_KEY_ID = "{AWS_EXAMPLE_KEY}"'
    assert "LIKE '%{name}%'" in users.line_text(21)
    assert 10 in users.added_lines
    assert 13 not in users.added_lines  # unchanged context line ...
    assert 13 in users.commentable_lines  # ... but GitHub still accepts a comment on it


def test_added_and_deleted_files():
    diff = """\
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""
    new, old = parse_diff(diff)
    assert new.status == "added" and new.added_lines == {1, 2}
    assert old.status == "deleted" and should_skip(old, [], 1000) == "file deleted"


def test_removed_line_that_looks_like_header_is_content():
    diff = """\
diff --git a/q.sql b/q.sql
--- a/q.sql
+++ b/q.sql
@@ -1,2 +1,2 @@
--- old comment
+-- new comment
 SELECT 1;
"""
    (f,) = parse_diff(diff)
    assert f.added_lines == {1}
    assert [ln.kind for ln in f.hunks[0].lines] == ["-", "+", " "]


def test_rename_and_binary():
    diff = """\
diff --git a/a.txt b/b.txt
similarity index 100%
rename from a.txt
rename to b.txt
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
"""
    renamed, binary = parse_diff(diff)
    assert renamed.status == "renamed" and renamed.path == "b.txt" and renamed.old_path == "a.txt"
    assert binary.is_binary


def test_skip_rules(sample_diff):
    files = {f.path: f for f in parse_diff(sample_diff)}
    assert "skip pattern" in should_skip(files["package-lock.json"], ["package-lock.json"], 1000)
    assert should_skip(files["app/users.py"], ["*.lock"], 1000) is None
    assert "too large" in should_skip(files["app/users.py"], [], 5)


def test_render_has_gutter(sample_diff):
    users = next(f for f in parse_diff(sample_diff) if f.path == "app/users.py")
    text = render_for_prompt(users)
    assert "   10 + AWS_ACCESS_KEY_ID" in text
    assert text.startswith("### File: app/users.py")


def test_chunking_respects_budget(sample_diff):
    files = parse_diff(sample_diff)
    assert len(chunk_files(files, 10)) == len(files)  # tiny budget: one file per chunk
    assert len(chunk_files(files, 1_000_000)) == 1
