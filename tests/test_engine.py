import asyncio
from types import SimpleNamespace

import pytest

from reviewbot.config import Settings
from reviewbot.engine import ReviewEngine
from reviewbot.llm import ClaudeClient, MockClient, ReviewRequest
from reviewbot.models import Category, LLMFinding, ReviewerOutput, Severity, Usage
from reviewbot.prompts import SHARED_SYSTEM, SPECIALISTS


def run(coro):
    return asyncio.run(coro)


def test_mock_end_to_end(sample_diff, settings):
    result = run(ReviewEngine(MockClient(), settings).review_diff(sample_diff))
    by_line = {f.line: f for f in result.findings}
    assert by_line[10].reviewers == ["secret-scanner"]
    assert by_line[21].severity == Severity.critical
    assert by_line[29].category == Category.correctness
    assert 37 in by_line and 40 in by_line
    assert result.files_skipped == {"package-lock.json": "matches skip pattern 'package-lock.json'"}
    assert result.usage.calls == 3


def test_failing_specialist_degrades_gracefully(sample_diff, settings):
    class Flaky(MockClient):
        async def review(self, request):
            if request.specialist.name == "security":
                raise TimeoutError("upstream timeout")
            return await super().review(request)

    result = run(ReviewEngine(Flaky(), settings).review_diff(sample_diff))
    assert result.errors == ["security: upstream timeout"]
    assert any(f.category == Category.correctness for f in result.findings)
    assert any(f.reviewers == ["secret-scanner"] for f in result.findings)  # deterministic layer still ran


def test_off_lane_findings_are_discarded(sample_diff, settings):
    class Wanderer:
        model = "test"

        async def review(self, request):
            stray = LLMFinding(path="app/users.py", line=21, severity=Severity.low,
                               category=Category.maintainability, title="Rename variable",
                               explanation="x", confidence=0.99)
            return ([stray] if request.specialist.name == "security" else []), Usage(calls=1)

    settings.specialists = ["security"]
    result = run(ReviewEngine(Wanderer(), settings).review_diff(sample_diff))
    assert [f for f in result.findings if f.reviewers != ["secret-scanner"]] == []


def test_unknown_specialist_rejected():
    with pytest.raises(ValueError):
        ReviewEngine(MockClient(), Settings(specialists=["vibes"]))


def test_claude_client_request_shape_and_parsing(sample_diff):
    """Use a stub SDK so we can assert on exactly what would be sent to the Claude API."""
    captured = {}

    class StubMessages:
        async def parse(self, **kwargs):
            captured.update(kwargs)
            finding = LLMFinding(path="app/users.py", line=21, severity=Severity.critical,
                                 category=Category.security, title="SQL injection", explanation="x",
                                 confidence=0.95)
            return SimpleNamespace(
                stop_reason="end_turn",
                parsed_output=ReviewerOutput(findings=[finding]),
                usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                                      cache_creation_input_tokens=0, cache_read_input_tokens=900),
            )

    client = ClaudeClient(None, "claude-sonnet-5-5", sdk_client=SimpleNamespace(messages=StubMessages()))
    from reviewbot.diff_parser import parse_diff

    files = [f for f in parse_diff(sample_diff) if f.path == "app/users.py"]
    findings, usage = run(client.review(ReviewRequest(SPECIALISTS["security"], files)))

    assert captured["model"] == "claude-sonnet-5-5"
    assert captured["system"] == SHARED_SYSTEM
    assert captured["output_format"] is ReviewerOutput
    diff_block, brief = captured["messages"][0]["content"]
    assert diff_block["cache_control"] == {"type": "ephemeral"}
    assert diff_block["text"].startswith("<diff>")
    assert "SECURITY reviewer" in brief["text"]
    assert findings[0].line == 21 and usage.cache_read_input_tokens == 900


def test_claude_client_raises_on_truncation(sample_diff):
    class Truncated:
        async def parse(self, **kwargs):
            return SimpleNamespace(stop_reason="max_tokens", parsed_output=None,
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    client = ClaudeClient(None, "m", sdk_client=SimpleNamespace(messages=Truncated()))
    with pytest.raises(RuntimeError, match="truncated"):
        run(client.review(ReviewRequest(SPECIALISTS["security"], [])))


def test_warm_cache_runs_first_specialist_alone(sample_diff, settings):
    order = []

    class Recorder:
        model = "claude-sonnet-5-5"
        supports_prompt_cache = True

        async def review(self, request):
            order.append(("start", request.specialist.name))
            await asyncio.sleep(0.01)
            order.append(("end", request.specialist.name))
            return [], Usage(calls=1)

    settings.warm_cache = True
    run(ReviewEngine(Recorder(), settings).review_diff(sample_diff))
    assert order[:2] == [("start", "security"), ("end", "security")]
    # the remaining specialists start together, after the cache is written
    assert order[2:4] == [("start", "correctness"), ("start", "maintainability")]
