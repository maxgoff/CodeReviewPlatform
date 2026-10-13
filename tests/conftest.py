from pathlib import Path

import pytest

from reviewbot.config import Settings
from tests.fakes import WEBHOOK_SECRET

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sample_diff() -> str:
    return (ROOT / "samples" / "vulnerable_pr.diff").read_text()


@pytest.fixture
def settings() -> Settings:
    return Settings(use_mock_llm=True, github_webhook_secret=WEBHOOK_SECRET, github_dry_run=True)
