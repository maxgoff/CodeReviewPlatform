from reviewbot.diff_parser import parse_diff
from reviewbot.secret_scan import REDACTED, scan_and_redact
from tests.fakes import AWS_EXAMPLE_KEY, FAKE_GITHUB_TOKEN, FAKE_PASSWORD

QUOTE = '"'


def test_flags_and_redacts_aws_key(sample_diff):
    files = parse_diff(sample_diff)
    findings = scan_and_redact(files)
    assert [(f.path, f.line, f.severity.value) for f in findings] == [("app/users.py", 10, "critical")]
    users = next(f for f in files if f.path == "app/users.py")
    assert AWS_EXAMPLE_KEY not in users.line_text(10)
    assert REDACTED in users.line_text(10)


def test_generic_password_redacts_value_only():
    diff = (
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -1 +1,2 @@\n x = 1\n"
        f"+db_password = {QUOTE}{FAKE_PASSWORD}{QUOTE}\n"
    )
    files = parse_diff(diff)
    (finding,) = scan_and_redact(files)
    assert finding.severity.value == "high"
    line = files[0].line_text(2)
    assert line.startswith("db_password = ") and REDACTED in line and FAKE_PASSWORD not in line


def test_secret_on_context_line_is_redacted_but_not_reported():
    diff = (
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -1 +1,2 @@\n"
        f' token = "{FAKE_GITHUB_TOKEN}"\n+y = 2\n'
    )
    files = parse_diff(diff)
    assert scan_and_redact(files) == []
    assert FAKE_GITHUB_TOKEN not in files[0].line_text(1)
