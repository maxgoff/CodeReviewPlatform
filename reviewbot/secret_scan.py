"""Deterministic secret detection and redaction. Runs before any code leaves your network.

Two jobs:
1. Flag hardcoded credentials as findings. Regexes are cheap, fast and never hallucinate.
2. Redact the secret value in the diff so it is never sent to a third-party model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .diff_parser import FileChange
from .models import Category, Finding, Severity


@dataclass(frozen=True)
class SecretRule:
    name: str
    pattern: re.Pattern[str]
    severity: Severity
    value_group: int = 0  # which regex group holds the secret value to redact


RULES = [
    SecretRule("AWS access key ID", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), Severity.critical),
    SecretRule("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), Severity.critical),
    SecretRule("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), Severity.critical),
    SecretRule("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), Severity.critical),
    SecretRule("Private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), Severity.critical),
    SecretRule(
        "Hardcoded credential",
        re.compile(
            r"""(?i)\w*(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\w*"""
            r"""['"]?\s*[:=]\s*(['"])([^'"\s]{8,})\1"""
        ),
        Severity.high,
        value_group=2,
    ),
]

REDACTED = "[REDACTED-SECRET]"


def scan_and_redact(changes: list[FileChange]) -> list[Finding]:
    """Mutates `changes` in place (redacting values) and returns one finding per detected secret."""
    findings: list[Finding] = []
    for change in changes:
        for hunk in change.hunks:
            for line in hunk.lines:
                for rule in RULES:
                    match = rule.pattern.search(line.text)
                    if not match or REDACTED in match.group(rule.value_group):
                        continue
                    if line.kind == "+":
                        findings.append(
                            Finding(
                                path=change.path,
                                line=line.new_no,
                                severity=rule.severity,
                                category=Category.security,
                                title=f"{rule.name} committed to source",
                                explanation=(
                                    "Secrets in git history are effectively public. Revoke and rotate this "
                                    "credential, then load it from a secret manager or environment variable."
                                ),
                                suggestion="",
                                confidence=0.95,
                                reviewers=["secret-scanner"],
                            )
                        )
                    start, end = match.span(rule.value_group)
                    line.text = line.text[:start] + REDACTED + line.text[end:]
    return findings
