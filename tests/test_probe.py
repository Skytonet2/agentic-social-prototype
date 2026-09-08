"""The connection probe. It must report the truth and write nothing."""

from __future__ import annotations

import pytest

from multiagency import db, pipeline
from multiagency.errors import HermesError
from multiagency.hermes import HermesRequest, HermesResponse
from multiagency.probe import PLACEHOLDER_MATERIAL, build_probe_request, render, run_probe


class StubHermes:
    """Stands in for HttpHermes at the call_raw boundary."""

    def __init__(self, payload=None, raise_with=None):
        self.payload = payload
        self.raise_with = raise_with
        self.calls: list[HermesRequest] = []

    def call_raw(self, request: HermesRequest):
        self.calls.append(request)
        if self.raise_with:
            raise self.raise_with
        if callable(self.payload):
            return self.payload(request)
        return self.payload


def good_payload(request: HermesRequest) -> dict:
    return {
        "text": "A short clean post about failing loudly.",
        "lane_id": request.lane.id,
        "material_id": request.material.id,
        "reasoning": "It names a specific failure, which is what this lane is for.",
    }


def test_the_probe_sends_the_real_contract(conn, cfg):
    pipeline.run_pull(conn, cfg)
    hermes = StubHermes(good_payload)

    run_probe(conn, cfg, hermes, "field_notes")

    sent = hermes.calls[0]
    assert sent.lane.id == "field_notes"
    assert sent.lane.purpose and sent.lane.example
    assert sent.constraints.max_length == 200
    assert "One idea per post." in sent.constraints.rules
    assert sent.material.content, "real material, not a placeholder"
    assert sent.retry_note is None


def test_the_probe_includes_published_history(conn, cfg, slot, when):
    from multiagency.hermes import MockHermes
    from multiagency.publisher import MockPublisher

    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    pipeline.approve(conn, outcome.post_id)
    pipeline.publish_due(conn, cfg, MockPublisher(), now=when)

    hermes = StubHermes(good_payload)
    run_probe(conn, cfg, hermes, "field_notes")

    assert len(hermes.calls[0].recent_posts) == 1


def test_the_probe_works_before_anything_has_been_pulled(conn, cfg):
    """A fresh checkout has no material, and the probe still has to work."""
    request = build_probe_request(conn, cfg, "field_notes")
    assert request.material == PLACEHOLDER_MATERIAL


def test_a_good_answer_passes(conn, cfg):
    pipeline.run_pull(conn, cfg)
    result = run_probe(conn, cfg, StubHermes(good_payload), "field_notes")

    assert result.ok
    assert isinstance(result.response, HermesResponse)
    assert result.constraint_summary is None
    assert "ok  text" in render(result, show_payload=False)


def test_a_wrong_shape_is_reported_with_the_raw_response(conn, cfg):
    pipeline.run_pull(conn, cfg)
    result = run_probe(
        conn, cfg, StubHermes({"choices": [{"message": {"content": "hi"}}]}), "field_notes"
    )

    assert not result.ok
    assert "missing text" in result.contract_error
    report = render(result, show_payload=False)
    assert "choices" in report, "the operator sees what actually came back"
    assert "call_raw" in report, "and where to map it"


def test_a_transport_failure_is_reported(conn, cfg):
    pipeline.run_pull(conn, cfg)
    result = run_probe(
        conn, cfg, StubHermes(raise_with=HermesError("connection refused")), "field_notes"
    )

    assert not result.ok
    assert "connection refused" in result.transport_error
    assert "FAILED before Hermes answered" in render(result, show_payload=False)


def test_a_constraint_violation_is_reported_but_is_not_a_contract_failure(conn, cfg):
    """A post that breaks a rule is still a working connection."""
    pipeline.run_pull(conn, cfg)

    def with_em_dash(request):
        payload = good_payload(request)
        payload["text"] = "A post with an em dash — like this."
        return payload

    result = run_probe(conn, cfg, StubHermes(with_em_dash), "field_notes")

    assert result.ok, "the contract held"
    assert "em dash" in result.constraint_summary
    assert "regeneration" in render(result, show_payload=False)


def test_the_probe_writes_nothing(conn, cfg):
    pipeline.run_pull(conn, cfg)
    material_before = db.next_unused_material(conn, "field_notes")["id"]

    run_probe(conn, cfg, StubHermes(good_payload), "field_notes")

    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert db.next_unused_material(conn, "field_notes")["id"] == material_before
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind != 'material_pulled'").fetchone()[0] == 0


def test_the_payload_can_be_shown_for_review(conn, cfg):
    pipeline.run_pull(conn, cfg)
    result = run_probe(conn, cfg, StubHermes(good_payload), "field_notes")

    report = render(result, show_payload=True)
    assert "--- request payload ---" in report
    assert '"purpose"' in report and '"recent_posts"' in report
