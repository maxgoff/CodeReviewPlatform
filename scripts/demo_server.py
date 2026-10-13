"""Run the ReviewBot UI fully offline: no GitHub, no API key.

    python scripts/demo_server.py          # then open http://localhost:27183

Any repository you enter is served from samples/sample_repo, and reviews use the mock reviewer.
Useful for workshops without reliable Wi-Fi, and for trying the UI before setting anything up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reviewbot.app import DEFAULT_PORT, create_app  # noqa: E402
from reviewbot.config import Settings  # noqa: E402
from reviewbot.engine import ReviewEngine  # noqa: E402
from reviewbot.llm import MockClient  # noqa: E402
from reviewbot.repo_review import RepoReviewer  # noqa: E402
from reviewbot.repo_source import read_directory  # noqa: E402

SAMPLE = ROOT / "samples" / "sample_repo"


class SampleRepoFetcher:
    """Pretends to be GitHub: every repository is the bundled sample."""

    async def fetch(self, spec, flt):
        snap = read_directory(SAMPLE, flt, spec.subpath)
        snap.label = f"{spec.full_name}@5a3c9e1"
        snap.commit = "5a3c9e1f0b7d2c4a8e6f1d3b5c7a9e0f2d4b6c8a"
        snap.repo_full_name = spec.full_name
        return snap


def main() -> None:
    import uvicorn

    settings = Settings(use_mock_llm=True, github_dry_run=True)
    engine = ReviewEngine(MockClient(), settings)
    app = create_app(settings, engine, repo_reviewer=RepoReviewer(engine, settings, SampleRepoFetcher()))
    port = int(os.getenv("REVIEWBOT_PORT", DEFAULT_PORT))
    print(f"Offline demo: http://localhost:{port}  (every repository is samples/sample_repo)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
