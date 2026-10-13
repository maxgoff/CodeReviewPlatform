"""Fake credentials for tests, assembled at runtime.

No secret-shaped string appears literally in the test sources, so secret scanners (GitHub push
protection, gitleaks, and ReviewBot's own scanner) find nothing to flag in this repository.
"""

# AWS's own documented placeholder key ID. It grants no access. samples/ plants it on purpose so the
# secret-scanner demo has something to catch; tests need the same value to check the parser and redaction.
AWS_EXAMPLE_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"

# A GitHub-token-shaped string with no real token behind it.
FAKE_GITHUB_TOKEN = "gh" + "p_" + "x" * 36

# A password-shaped value for the generic "hardcoded credential" rule.
FAKE_PASSWORD = "y" * 14

# HMAC key the webhook tests sign with.
WEBHOOK_SECRET = "-".join(["test", "webhook", "key"])
