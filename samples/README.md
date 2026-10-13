# Samples

Fixtures for demos, tests and evals. The bugs in them are deliberate.

- `vulnerable_pr.diff`: a pull request with SQL injection, command injection, an off-by-one error,
  a swallowed exception and a "hardcoded" AWS key.
- `sample_repo/`: a tiny repository with the same problems plus a prompt-injection comment.

The AWS key ID in both (the one ending in `EXAMPLE`) is the placeholder Amazon publishes in its own
documentation. It is not a credential and grants no access; it is there so the secret scanner has
something to find. No other secret-shaped values exist in this repository: the tests build their fake
tokens at runtime (see `tests/fakes.py`).
