from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from multiagency import db  # noqa: E402
from multiagency.config import load_config  # noqa: E402

UTC = timezone.utc

CONFIG_YAML = """
timezone: UTC
pull_interval_minutes: 60
generate_lead_hours: 36
global_constraints:
  max_length: 280
  allow_emojis: false
  forbid_em_dash: true
  rules:
    - "Do not use em-dashes."
    - "No claims about funding, grants or bounties."
lanes:
  - id: field_notes
    audience: builders
    post_type: observation
    purpose: One concrete thing learned while building.
    example: Agents that retry silently are worse than agents that fail loudly.
    constraints:
      max_length: 200
      allow_emojis: false
      rules:
        - "One idea per post."
    active: true
    sources:
      - id: notes_file
        source_type: file_lines
        location: {notes}
schedule:
  - id: notes_tue
    lane_id: field_notes
    day_of_week: tue
    time: "10:00"
    active: true
"""

MATERIAL = [
    "Agents that retry silently are worse than agents that fail loudly, and the fix was making every failure write a reason.",
    "The bottleneck in agent work is almost never the model, it is the material going in.",
    "Dedupe on content, not on identifier, because half of what we pulled twice had a new id.",
    "Storing why a model picked something turned out to be worth more than storing what it wrote.",
]


@pytest.fixture
def notes_file(tmp_path: Path) -> Path:
    path = tmp_path / "notes.txt"
    path.write_text(
        "# a comment that should be ignored\n" + "\n".join(MATERIAL) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config_path(tmp_path: Path, notes_file: Path) -> Path:
    path = tmp_path / "content.yaml"
    path.write_text(
        CONFIG_YAML.format(notes=notes_file.as_posix()), encoding="utf-8"
    )
    return path


@pytest.fixture
def cfg(config_path: Path):
    return load_config(config_path)


@pytest.fixture
def conn(tmp_path: Path, cfg):
    connection = db.connect(tmp_path / "test.db")
    db.sync_config(connection, cfg)
    yield connection
    connection.close()


@pytest.fixture
def slot(cfg):
    return cfg.active_slots[0]


@pytest.fixture
def when() -> datetime:
    """A Tuesday at 10:00 UTC, matching the slot in the test config."""
    return datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
