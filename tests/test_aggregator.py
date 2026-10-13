from reviewbot.aggregator import aggregate, title_similarity
from reviewbot.diff_parser import parse_diff
from reviewbot.models import Category, Finding, Severity


def make(line, title="SQL injection in query", sev=Severity.high, cat=Category.security, conf=0.9, who="security",
         path="app/users.py"):
    return Finding(path=path, line=line, severity=sev, category=cat, title=title, explanation="x",
                   confidence=conf, reviewers=[who])


def files(sample_diff):
    return parse_diff(sample_diff)


def test_drops_lines_outside_diff_and_unknown_files(sample_diff):
    res = aggregate([make(21), make(500), make(21, path="nope.py")], files(sample_diff), 0.7, 10)
    assert len(res.kept) == 1
    assert res.dropped == {"line_not_in_diff": 1, "unknown_file": 1}


def test_confidence_gate(sample_diff):
    res = aggregate([make(21, conf=0.95), make(29, title="Off by one", cat=Category.correctness, conf=0.5)],
                    files(sample_diff), 0.7, 10)
    assert [f.line for f in res.kept] == [21]
    assert res.dropped["below_confidence"] == 1


def test_duplicates_merge_reviewers_and_keep_strongest(sample_diff):
    a = make(21, title="SQL query built with f-string", sev=Severity.low, cat=Category.maintainability,
             conf=0.75, who="maintainability")
    b = make(21, title="SQL query built with string formatting", sev=Severity.critical, conf=0.93)
    res = aggregate([a, b], files(sample_diff), 0.7, 10)
    (kept,) = res.kept
    assert kept.severity == Severity.critical
    assert kept.reviewers == ["maintainability", "security"]
    assert res.dropped["duplicate"] == 1


def test_different_issues_on_same_line_are_kept(sample_diff):
    a = make(37, title="Command injection via shell=True")
    b = make(37, title="Subprocess failure ignored", cat=Category.correctness, who="correctness")
    assert len(aggregate([a, b], files(sample_diff), 0.7, 10).kept) == 2


def test_ranking_and_cap(sample_diff):
    findings = [
        make(22, title="Debug print", sev=Severity.low, cat=Category.maintainability),
        make(21, sev=Severity.critical),
        make(29, title="Off by one", sev=Severity.high, cat=Category.correctness),
    ]
    res = aggregate(findings, files(sample_diff), 0.7, max_comments=2)
    assert [f.line for f in res.kept] == [21, 29]
    assert [f.line for f in res.overflow] == [22]


def test_title_similarity():
    assert title_similarity("SQL injection in query", "SQL injection risk") > 0.3
    assert title_similarity("Debug print", "Off by one") == 0
