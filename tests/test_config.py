"""The config must fail loudly, with every problem, before anything starts."""

from __future__ import annotations

import pytest

from multiagency.config import load_config
from multiagency.errors import ConfigError


def test_valid_config_loads(cfg):
    assert [lane.id for lane in cfg.lanes] == ["field_notes"]
    assert cfg.lanes[0].sources[0].source_type == "file_lines"
    assert [slot.id for slot in cfg.active_slots] == ["notes_tue"]


def test_lane_constraints_fold_onto_global(cfg):
    merged = cfg.constraints_for("field_notes")
    assert merged.max_length == 200, "the tighter lane limit wins"
    assert merged.forbid_em_dash is True
    assert "Do not use em-dashes." in merged.rules
    assert "One idea per post." in merged.rules


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_every_problem_is_reported_at_once(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
timezone: Mars/Olympus
global_constraints:
  max_length: 400
lanes:
  - id: Bad-Id
    audience: nobody
    post_type: thing
    sources: []
schedule:
  - id: s1
    lane_id: missing_lane
    day_of_week: funday
    time: 9:00
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)

    message = str(exc.value)
    for expected in [
        "above the platform limit",
        "must be lowercase letters",
        "must be one of ['builders', 'clients', 'everyone', 'projects']",
        "purpose: is required and missing",
        "an active lane needs at least one source",
        "does not match any lane",
        "day_of_week: must be one of",
        "must be a quoted 24-hour HH:MM string",
        "not a timezone this machine knows",
    ]:
        assert expected in message, expected


def test_unquoted_time_is_caught(tmp_path, notes_file):
    """9:00 in YAML parses as the integer 540, which is the classic trap."""
    from tests.conftest import CONFIG_YAML

    path = tmp_path / "content.yaml"
    path.write_text(
        CONFIG_YAML.format(notes=notes_file.as_posix()).replace('"10:00"', "10:00"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="quoted 24-hour"):
        load_config(path)


def test_yaml_syntax_error_is_readable(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("lanes: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)
