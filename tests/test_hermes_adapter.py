"""The live adapter: what goes over the wire, and what happens when it breaks."""

from __future__ import annotations

import json

import pytest
import requests

from multiagency.errors import HermesError
from multiagency.hermes import HttpHermes, MaterialBrief, build_request, get_hermes
from multiagency.settings import Settings


@pytest.fixture
def request_(cfg):
    return build_request(
        cfg,
        "field_notes",
        MaterialBrief(id=3, source_id="notes_file", content="Something we learned."),
    )


class FakeResponse:
    """Enough of a requests.Response for the paths _call cares about."""

    def __init__(self, status_code: int = 200, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def stub_session(hermes: HttpHermes, response=None, raise_with=None) -> dict:
    """Replace the session so nothing reaches the network, and record the call."""
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        if raise_with:
            raise raise_with
        return response

    hermes.session.post = fake_post
    return captured


GOOD_PAYLOAD = {
    "text": "A post.",
    "lane_id": "field_notes",
    "material_id": 3,
    "reasoning": "It is the clearest thing in the queue.",
}


def test_the_whole_contract_goes_over_the_wire(request_):
    hermes = HttpHermes("https://hermes.example/generate", "k")
    captured = stub_session(hermes, FakeResponse(200, GOOD_PAYLOAD))

    response = hermes.generate(request_)

    body = captured["json"]
    assert set(body) == {"lane", "constraints", "material", "recent_posts", "retry_note"}
    assert body["lane"]["purpose"] and body["lane"]["example"]
    assert body["constraints"]["max_length"] == 200
    assert "Do not use em-dashes." in body["constraints"]["rules"]
    assert body["material"]["id"] == 3
    assert captured["url"] == "https://hermes.example/generate"
    assert captured["timeout"] == 60
    assert hermes.session.headers["Authorization"] == "Bearer k"
    assert response.text == "A post."
    assert response.reasoning.startswith("It is the clearest")


def test_no_authorization_header_without_a_key(request_):
    hermes = HttpHermes("https://hermes.example/generate")
    assert "Authorization" not in hermes.session.headers


def test_the_retry_note_is_sent_on_a_regeneration(cfg, request_):
    from multiagency.hermes import build_request as build

    hermes = HttpHermes("https://hermes.example/generate")
    captured = stub_session(hermes, FakeResponse(200, GOOD_PAYLOAD))
    retry = build(
        cfg,
        "field_notes",
        request_.material,
        retry_note="The previous attempt contained an em dash.",
    )

    hermes.generate(retry)

    assert captured["json"]["retry_note"] == "The previous attempt contained an em dash."


def test_a_response_outside_the_contract_is_an_error(request_):
    hermes = HttpHermes("https://hermes.example/generate")
    stub_session(
        hermes,
        FakeResponse(200, {"text": "A post.", "lane_id": "field_notes", "material_id": 3}),
    )
    with pytest.raises(HermesError, match="reasoning"):
        hermes.generate(request_)


def test_non_json_is_an_error(request_):
    hermes = HttpHermes("https://hermes.example/generate")
    stub_session(hermes, FakeResponse(200, None, text="<html>gateway timeout</html>"))
    with pytest.raises(HermesError, match="not JSON"):
        hermes.generate(request_)


def test_an_http_error_is_reported_with_its_body(request_):
    hermes = HttpHermes("https://hermes.example/generate")
    stub_session(hermes, FakeResponse(503, None, text="upstream unavailable"))
    with pytest.raises(HermesError, match="503.*upstream unavailable"):
        hermes.generate(request_)


def test_an_unreachable_endpoint_is_reported(request_):
    hermes = HttpHermes("https://hermes.example/generate")
    stub_session(hermes, raise_with=requests.ConnectionError("connection refused"))
    with pytest.raises(HermesError, match="could not reach Hermes"):
        hermes.generate(request_)


def test_a_timeout_is_reported(request_):
    hermes = HttpHermes("https://hermes.example/generate")
    stub_session(hermes, raise_with=requests.Timeout("timed out"))
    with pytest.raises(HermesError, match="could not reach Hermes"):
        hermes.generate(request_)


def test_a_wrapped_response_can_be_mapped_by_overriding_call_raw(request_):
    """The seam documented in docs/hermes-contract.md, exercised.

    Hermes will most likely wrap a model, and models come with envelopes.
    Unwrapping belongs in call_raw and nothing above it should notice.
    """

    class WrappedHermes(HttpHermes):
        def call_raw(self, request):
            raw = super().call_raw(request)
            return json.loads(raw["choices"][0]["message"]["content"])

    hermes = WrappedHermes("https://hermes.example/generate")
    stub_session(
        hermes,
        FakeResponse(
            200,
            {"choices": [{"message": {"content": json.dumps(GOOD_PAYLOAD)}}]},
        ),
    )

    response = hermes.generate(request_)

    assert response.text == "A post."
    assert response.lane_id == "field_notes"
    assert response.material_id == 3
    assert response.reasoning.startswith("It is the clearest")


def _settings(**overrides) -> Settings:
    base = dict(
        config_path="config/content.yaml",
        db_path="data/x.db",
        hermes_mode="mock",
        hermes_endpoint="",
        hermes_api_key="",
        publisher="mock",
        ui_username="",
        ui_password="",
    )
    base.update(overrides)
    return Settings(**base)


def test_live_mode_without_an_endpoint_fails_loudly():
    with pytest.raises(HermesError, match="HERMES_ENDPOINT is not set"):
        get_hermes(_settings(hermes_mode="live"))


def test_unknown_mode_fails_loudly():
    with pytest.raises(HermesError, match="expected 'mock' or 'live'"):
        get_hermes(_settings(hermes_mode="whatever"))


def test_mock_mode_is_the_default():
    from multiagency.hermes import MockHermes

    assert isinstance(get_hermes(_settings()), MockHermes)
