"""The contract is the part worth guarding: what Hermes gets and what it owes."""

from __future__ import annotations

import pytest

from multiagency.errors import HermesError
from multiagency.hermes import (
    HermesResponse,
    MaterialBrief,
    MockHermes,
    PublishedPost,
    build_request,
)
from multiagency.hermes.contract import RECENT_POST_WINDOW


@pytest.fixture
def material() -> MaterialBrief:
    return MaterialBrief(id=7, source_id="notes_file", content="A thing we learned.")


def test_request_carries_the_lane_definition(cfg, material):
    request = build_request(cfg, "field_notes", material)
    assert request.lane.audience == "builders"
    assert request.lane.post_type == "observation"
    assert request.lane.purpose.startswith("One concrete thing")
    assert request.lane.example, "the worked example must be sent"


def test_request_carries_global_and_lane_constraints(cfg, material):
    request = build_request(cfg, "field_notes", material)
    payload = request.to_payload()
    assert payload["constraints"]["max_length"] == 200
    assert payload["constraints"]["forbid_em_dash"] is True
    assert "Do not use em-dashes." in payload["constraints"]["rules"]
    assert "One idea per post." in payload["constraints"]["rules"]


def test_request_carries_the_material(cfg, material):
    request = build_request(cfg, "field_notes", material)
    assert request.to_payload()["material"]["id"] == 7
    assert request.to_payload()["material"]["content"] == "A thing we learned."


def test_recent_posts_are_capped_at_ten(cfg, material):
    recent = [PublishedPost("field_notes", "post {}".format(i)) for i in range(25)]
    request = build_request(cfg, "field_notes", material, recent)
    assert len(request.recent_posts) == RECENT_POST_WINDOW == 10
    assert request.recent_posts[0].text == "post 0"


def test_response_requires_all_four_fields(cfg, material):
    request = build_request(cfg, "field_notes", material)
    for missing in ("text", "lane_id", "material_id", "reasoning"):
        payload = {
            "text": "fine",
            "lane_id": "field_notes",
            "material_id": 7,
            "reasoning": "because",
        }
        del payload[missing]
        with pytest.raises(HermesError, match=missing):
            HermesResponse.from_payload(payload, request)


def test_empty_reasoning_is_a_failure(cfg, material):
    """Reasoning is what makes review fast, so an empty one is not acceptable."""
    request = build_request(cfg, "field_notes", material)
    with pytest.raises(HermesError, match="reasoning"):
        HermesResponse.from_payload(
            {"text": "fine", "lane_id": "field_notes", "material_id": 7, "reasoning": "  "},
            request,
        )


def test_mismatched_lane_or_material_is_a_failure(cfg, material):
    request = build_request(cfg, "field_notes", material)
    with pytest.raises(HermesError, match="lane_id"):
        HermesResponse.from_payload(
            {"text": "t", "lane_id": "other", "material_id": 7, "reasoning": "r"}, request
        )
    with pytest.raises(HermesError, match="material_id"):
        HermesResponse.from_payload(
            {"text": "t", "lane_id": "field_notes", "material_id": 99, "reasoning": "r"},
            request,
        )


def test_empty_text_is_a_failure(cfg, material):
    request = build_request(cfg, "field_notes", material)
    with pytest.raises(HermesError, match="empty"):
        HermesResponse.from_payload(
            {"text": "   ", "lane_id": "field_notes", "material_id": 7, "reasoning": "r"},
            request,
        )


def test_mock_answers_within_the_contract(cfg, material):
    request = build_request(cfg, "field_notes", material)
    response = MockHermes().generate(request)
    assert response.lane_id == "field_notes"
    assert response.material_id == 7
    assert response.text
    assert response.reasoning


def test_mock_can_be_told_to_break_a_constraint(cfg, material):
    from multiagency.validation import validate

    request = build_request(cfg, "field_notes", material)
    hermes = MockHermes(violations=["em_dash"])
    first = hermes.generate(request)
    assert not validate(first.text, request.constraints).ok
    second = hermes.generate(request)
    assert validate(second.text, request.constraints).ok
