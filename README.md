# ReviewBot: an AI-powered code review platform (proof of concept)

ReviewBot reviews GitHub pull requests with a panel of specialist AI reviewers running on the Claude API,
then posts one consolidated review with inline comments. It can also download a whole repository and
review every source file in it, from the command line or from a web UI at **http://localhost:27183**.

```
GitHub PR ──webhook──▶ FastAPI /webhook ──▶ fetch diff ──▶ ReviewEngine ──▶ POST review + inline comments
                        (HMAC verified,                    │
                         202 in < 10 s)                    ├─ parse & filter (skip lockfiles, binaries, huge files)
                                                           ├─ secret scan + redact   (deterministic, local)
                                                           ├─ chunk to a token budget
                                                           ├─ specialists in parallel (Claude, structured output)
                                                           │     security · correctness · maintainability
                                                           └─ aggregate: location check → confidence gate
                                                                         → de-duplicate → rank → cap
```

It is the companion code for the workshop deck *Building an AI-Powered Code Review Platform*.

## Quick start (no API key needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                                                   # 53 tests
python scripts/demo_server.py                            # web UI, fully offline: http://localhost:27183
reviewbot review --diff samples/vulnerable_pr.diff --mock # CLI review with the offline reviewer
python -m evals.run_evals                                # precision / recall on labelled diffs
```

The mock reviewer is a deterministic pattern matcher that implements the same interface as the Claude
client. It exists so you can run the pipeline, tests and demos offline. It deliberately misses the
path-traversal bug in `evals/cases/prompt_injection.diff`, which shows what a regex cannot do.

## Use Claude

```bash
export ANTHROPIC_API_KEY=sk-ant-...
reviewbot review --diff samples/vulnerable_pr.diff
reviewbot review --git main...HEAD --fail-on high        # exit 1 on high/critical: use as a CI gate
python -m evals.run_evals --live                         # score the real reviewer
python -m evals.run_evals --live --model claude-opus-5-5 # compare models on the same cases
```

## Review a whole repository

Give it a GitHub repository (or a local folder). It downloads a snapshot of the code pinned to one commit,
shows what it will review and what that should cost, then runs the same specialist pipeline over every
source file and writes a report to `reviews/`.

```bash
reviewbot repo samples/sample_repo --mock                     # offline demo on the bundled sample
reviewbot repo pallets/itsdangerous --plan                    # list files + cost estimate, no model calls
reviewbot repo pallets/itsdangerous                           # review the default branch
reviewbot repo owner/repo@v2.1 --path src/api --include "*.py"
reviewbot repo https://github.com/owner/repo/tree/main/services/billing --budget 5
reviewbot repo ~/code/my-checkout --exclude "tests/*"
```

Accepted sources: `owner/repo`, `owner/repo@ref`, `https://github.com/owner/repo[/tree/<ref>/<path>]`,
`git@github.com:owner/repo.git`, or a local directory. Private repositories need `GITHUB_TOKEN`
(a fine-grained token with **Contents: Read** is enough).

Guardrails, because a repository can be far bigger than a pull request:

- **Only source files.** Known code and config extensions, minus dependency and build folders
  (`node_modules`, `vendor`, `dist`, ...), binaries, and files over 200 KB. Use `--include` / `--exclude` to change it.
- **A file-count limit** (`--max-files`, default 150). Over it, ReviewBot stops and lists the biggest
  directories so you can narrow with `--path`.
- **A cost budget** (`--budget`, default $2.00). The estimate assumes no cache hits, so real cost is usually lower.
  `--yes` skips the check. `--plan` shows the estimate without calling the model.
- **Safe download.** The tarball is read in memory, never extracted, so symlinks and `../` paths in a hostile
  archive are ignored. Downloads over 200 MB are refused.

The report groups findings by severity with links to the exact line on GitHub, lists the files with the most
findings, and records what was skipped and why.

### In the browser

```bash
reviewbot-server                                          # http://localhost:27183
```

Paste a repository, check the plan (files, lines, model calls, estimated cost against your budget, what was
skipped), then start the review and watch it progress. Findings are listed like annotated source, each with its
line number, severity and a link to the exact line on GitHub, and you can filter by severity, category, file or
text, and download the Markdown report.

The page talks only to its own server and treats everything in a finding as untrusted text: findings are
inserted as text, never HTML, and the page is served with a strict Content-Security-Policy.

No API key or network yet? `python scripts/demo_server.py` runs the same UI offline: every repository you
enter is served from `samples/sample_repo` and reviewed by the mock reviewer.

**Why port 27183?** It isn't registered with IANA and sits below the ephemeral port ranges macOS
(49152+) and Linux (32768+) hand out to outgoing connections, so it is unlikely to clash with anything.
Set `REVIEWBOT_PORT` to change it. The server listens on `127.0.0.1` only unless you set `REVIEWBOT_HOST`.

### Over HTTP

The server exposes the same feature. Jobs are kept in memory and run one at a time.

```bash
export REVIEWBOT_API_TOKEN=change-me      # without it, only requests from localhost are accepted
reviewbot-server &

curl -s -X POST localhost:27183/repo-reviews -H "Authorization: Bearer $REVIEWBOT_API_TOKEN" \
     -H "Content-Type: application/json" -d '{"repo": "pallets/itsdangerous", "path": "src"}'
# {"id": "3f2a...", "status": "queued", "status_url": "/repo-reviews/3f2a...", "report_url": ...}

curl -s localhost:27183/repo-reviews/3f2a... -H "Authorization: Bearer $REVIEWBOT_API_TOKEN"         # status, findings
curl -s localhost:27183/repo-reviews/3f2a.../report -H "Authorization: Bearer $REVIEWBOT_API_TOKEN"  # Markdown
```

Body fields: `repo` (required), `ref`, `path`, `include`, `exclude`, `max_files`, `budget_usd`, `plan_only`.
With `plan_only: true` the job stops at `planned`; `POST /repo-reviews/{id}/start` runs it. `GET /repo-reviews`
lists recent jobs and `GET /api/info` reports the model, budget and whether a token is required.
The API accepts GitHub repositories only, never server paths, and `REPO_BUDGET_USD` caps every request.

## Run the webhook service

Dry run (no GitHub needed). The server reviews `samples/vulnerable_pr.diff` and writes the review JSON to `out/`:

```bash
export GITHUB_DRY_RUN=true GITHUB_WEBHOOK_SECRET=dev-secret
reviewbot-server &                                       # http://localhost:27183
python scripts/simulate_webhook.py                       # sends a signed pull_request event
cat out/review_acme_accounts_7.json
```

Against a real repository:

1. Create a fine-grained personal access token with **Pull requests: Read and write** and **Contents: Read**
   on the test repo. For production, use a GitHub App installation token instead.
2. Expose the server: `ngrok http 27183` (or `npx smee-client`).
3. In the repo: *Settings → Webhooks → Add webhook*. Payload URL `https://<tunnel>/webhook`,
   content type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`, event **Pull requests**.
4. Run the server with `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` and `ANTHROPIC_API_KEY` set
   (copy `.env.example`). Open a PR.

Docker: `docker build -t reviewbot . && docker run -p 27183:27183 --env-file .env reviewbot`

As a GitHub Action instead of a service: copy `examples/github-action/reviewbot.yml` to `.github/workflows/` in the target repo and add an `ANTHROPIC_API_KEY` secret.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Claude API key. If unset, the mock reviewer is used. |
| `REVIEW_MODEL` | `claude-sonnet-5-5` | Model for the specialists. |
| `REVIEW_SPECIALISTS` | `security,correctness,maintainability` | Which reviewers run. |
| `REVIEW_MIN_CONFIDENCE` | `0.7` | Findings below this are dropped. Tune with the evals. |
| `REVIEW_MAX_COMMENTS` | `15` | Inline comment cap; the rest go in the summary. |
| `REVIEW_MAX_FILE_LINES` | `1500` | Files with more changed lines are skipped. |
| `REVIEW_CHUNK_CHAR_BUDGET` | `60000` | Max diff characters per request; large PRs are split. |
| `REVIEW_WARM_CACHE` | `true` | Run one specialist first so the others read the diff from prompt cache. |
| `GITHUB_TOKEN` | | Token used to read diffs and post reviews. |
| `GITHUB_WEBHOOK_SECRET` | | HMAC secret; requests without a valid signature get 401. |
| `GITHUB_DRY_RUN` | `true` if no token | Read a local diff and write reviews to disk. |
| `REVIEW_MAX_CONCURRENCY` | `4` | Chunks reviewed in parallel (repository reviews). |
| `REPO_MAX_FILES` | `150` | Default `--max-files`. |
| `REPO_BUDGET_USD` | `2.00` | Default `--budget`; hard ceiling for API requests. |
| `REPO_MAX_FILE_BYTES` | `200000` | Larger files are skipped. |
| `REPO_MAX_DOWNLOAD_MB` | `200` | Larger tarballs are refused. |
| `REPO_MAX_FINDINGS` | `100` | Findings listed in full; the rest are counted. |
| `REPO_OUTPUT_DIR` | `reviews` | Where CLI reports are written. |
| `REVIEWBOT_API_TOKEN` | | Bearer token for `/repo-reviews` and the UI. Unset means localhost only. |
| `REVIEWBOT_PORT` | `27183` | Port for the UI, API and webhook. |
| `REVIEWBOT_HOST` | `127.0.0.1` | Interface to listen on. Docker sets `0.0.0.0`. |

## Project layout

```
reviewbot/
  diff_parser.py   unified diff → files, hunks, line numbers; numbered rendering for prompts
  secret_scan.py   regex secret detection + redaction before anything leaves the network
  prompts.py       shared system prompt + specialist briefs (cache-friendly layout)
  llm.py           LLMClient protocol, ClaudeClient (structured outputs), MockClient
  engine.py        orchestration: filter → scan → chunk → parallel specialists → aggregate
  aggregator.py    location check, confidence gate, de-duplication, ranking, cap
  report.py        GitHub comment bodies, summary Markdown, terminal output
  github.py        signature check, diff fetch, review posting (with 422 fallback), dry-run client
  app.py           FastAPI webhook: verify, filter events, de-dupe deliveries, background review
  repo_source.py   repo spec parsing, GitHub tarball download, safe in-memory extraction, file filters
  repo_review.py   whole-repo flow: load → plan → file-count and budget guards → review → report
  repo_api.py      /repo-reviews job API: plan, start, status, report
  ui/              web UI: index.html, app.css, app.js (no build step, no external requests)
  cli.py           `reviewbot review` (diffs, CI) and `reviewbot repo` (whole repositories)
evals/             labelled diffs + scorer (recall, precision, F1)
tests/             unit and integration tests (stubbed Claude SDK, fake GitHub)
samples/           sample PR diff and a small sample repository used by demos and tests
scripts/           simulate_webhook.py (signed test event), demo_server.py (offline UI demo)
```

## Design decisions worth knowing

- **Line numbers come from a gutter, not from the model.** The prompt shows each new-file line number
  next to the code and the schema asks the model to copy it. The aggregator then rejects any finding that
  points at a line not in the diff, which also stops GitHub from rejecting the review.
- **Structured outputs, not "please reply in JSON".** `messages.parse(output_format=ReviewerOutput)` constrains
  the response to the Pydantic schema, so parsing cannot fail on malformed JSON.
- **Cache-friendly prompt order.** System prompt and diff are identical across specialists and come first;
  the specialist brief comes last. Specialists two and three read the diff from cache.
- **The diff is untrusted input.** It is wrapped in `<diff>` tags, the system prompt says to treat it as data,
  the model has no tools, its output is schema-validated, and the bot can only `COMMENT`, never approve.
- **Humans own the merge.** The service never approves or requests changes. Use `--fail-on` in CI if you
  want a hard gate, and only after the evals show the precision to justify it.

## Where to take it next

- GitHub App auth, a job queue (SQS, Redis, Cloud Tasks) and a database of findings and reactions.
- Repository context: let a reviewer call tools to read the full file or search for callers.
- Feedback loop: record 👍/👎 on comments and feed it into the eval set.
- Incremental reviews on `synchronize`: review only commits since the last reviewed SHA.
- Repo-specific rules from a `REVIEWBOT.md` in the target repository appended to the shared prompt.
