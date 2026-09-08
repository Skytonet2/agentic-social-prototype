"""Images: Hermes decides, the renderer executes, the human still approves."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import requests

from multiagency import db, pipeline
from multiagency.errors import ImageError
from multiagency.hermes import HermesResponse, MaterialBrief, MockHermes, build_request
from multiagency.images import (
    MockImageRenderer,
    OpenAIImageRenderer,
    get_renderer,
    _solid_png,
)
from multiagency.settings import Settings


# --- the renderer makes no decisions --------------------------------------


def test_the_mock_renderer_writes_a_real_png(tmp_path, monkeypatch):
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    image = MockImageRenderer().render("a quiet diagram", post_id=7)

    assert image.path.exists()
    assert image.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert image.mime == "image/png"
    assert image.prompt == "a quiet diagram"


def test_the_same_prompt_renders_the_same_image(tmp_path, monkeypatch):
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    first = MockImageRenderer().render("one prompt", post_id=1).read()
    second = MockImageRenderer().render("one prompt", post_id=2).read()
    different = MockImageRenderer().render("another prompt", post_id=3).read()

    assert first == second
    assert first != different, "different prompts are visibly different in review"


def test_the_generated_png_is_well_formed():
    png = _solid_png(200, 100, 50, size=8)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png.endswith(b"IEND\xaeB`\x82")


# --- the OpenAI renderer --------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def stub(renderer, response=None, raise_with=None) -> dict:
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        if raise_with:
            raise raise_with
        return response

    renderer.session.post = fake_post
    return captured


def _b64_png() -> str:
    import base64

    return base64.b64encode(_solid_png(1, 2, 3, size=4)).decode()


def test_the_prompt_goes_to_the_model_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    renderer = OpenAIImageRenderer("key", model="gpt-image-1", size="1024x1024", quality="medium")
    captured = stub(renderer, FakeResponse(200, {"data": [{"b64_json": _b64_png()}]}))

    image = renderer.render("draw the thing Hermes asked for", post_id=4)

    assert captured["json"]["prompt"] == "draw the thing Hermes asked for", (
        "the renderer must not rewrite or embellish the prompt"
    )
    assert captured["json"]["model"] == "gpt-image-1"
    assert captured["json"]["size"] == "1024x1024"
    assert captured["json"]["n"] == 1
    assert renderer.session.headers["Authorization"] == "Bearer key"
    assert image.path.exists()


def test_a_missing_key_fails_loudly():
    with pytest.raises(ImageError, match="OPENAI_API_KEY"):
        OpenAIImageRenderer("")


def test_an_http_error_becomes_an_image_error(tmp_path, monkeypatch):
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    renderer = OpenAIImageRenderer("key")
    stub(renderer, FakeResponse(429, text="rate limit"))
    with pytest.raises(ImageError, match="429.*rate limit"):
        renderer.render("anything", post_id=1)


def test_an_unreachable_model_becomes_an_image_error(tmp_path, monkeypatch):
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    renderer = OpenAIImageRenderer("key")
    stub(renderer, raise_with=requests.ConnectionError("no route"))
    with pytest.raises(ImageError, match="could not reach the image model"):
        renderer.render("anything", post_id=1)


def test_a_url_only_response_is_refused(tmp_path, monkeypatch):
    """A URL that expires is not something to hang a queued post on."""
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    renderer = OpenAIImageRenderer("key")
    stub(renderer, FakeResponse(200, {"data": [{"url": "https://example.com/i.png"}]}))
    with pytest.raises(ImageError, match="b64_json"):
        renderer.render("anything", post_id=1)


def _settings(**overrides) -> Settings:
    base = dict(
        config_path="config/content.yaml", db_path="data/x.db", hermes_mode="mock",
        hermes_endpoint="", hermes_api_key="", publisher="mock",
        image_renderer="mock", openai_api_key="", ui_username="", ui_password="",
    )
    base.update(overrides)
    return Settings(**base)


def test_unknown_renderer_fails_loudly(cfg):
    with pytest.raises(ImageError, match="expected 'mock' or 'openai'"):
        get_renderer(_settings(image_renderer="midjourney"), cfg)


# --- the contract ---------------------------------------------------------


def test_an_image_prompt_without_alt_text_is_a_contract_error(cfg):
    request = build_request(
        cfg, "field_notes", MaterialBrief(id=1, source_id="s", content="c")
    )
    with pytest.raises(Exception, match="image_alt"):
        HermesResponse.from_payload(
            {
                "text": "t", "lane_id": "field_notes", "material_id": 1,
                "reasoning": "r", "image_prompt": "draw something",
            },
            request,
        )


def test_no_image_fields_is_perfectly_valid(cfg):
    request = build_request(
        cfg, "field_notes", MaterialBrief(id=1, source_id="s", content="c")
    )
    response = HermesResponse.from_payload(
        {"text": "t", "lane_id": "field_notes", "material_id": 1, "reasoning": "r"},
        request,
    )
    assert not response.wants_image


def test_the_lane_is_told_whether_images_are_allowed(cfg):
    request = build_request(
        cfg, "field_notes", MaterialBrief(id=1, source_id="s", content="c")
    )
    assert request.to_payload()["images"] == {"allowed": False, "alt_text_max": 1000}


# --- the pipeline ---------------------------------------------------------


def test_a_lane_that_disallows_images_drops_the_prompt(conn, cfg, slot, when, tmp_path,
                                                       monkeypatch):
    """The test lane has images off, so a prompt must not become a picture."""
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)

    class EagerHermes(MockHermes):
        def generate(self, request):
            response = super().generate(request)
            return HermesResponse(
                text=response.text, lane_id=response.lane_id,
                material_id=response.material_id, reasoning=response.reasoning,
                image_prompt="draw it anyway", image_alt="an image",
            )

    pipeline.run_pull(conn, cfg)
    renderer = MockImageRenderer()
    outcome = pipeline.generate_for_slot(conn, cfg, EagerHermes(), slot, when, renderer)

    row = db.get_post(conn, outcome.post_id)
    assert row["image_path"] is None
    assert renderer.calls == [], "the renderer was never asked"
    assert "image_not_allowed" in [e["kind"] for e in db.recent_events(conn)]


def test_a_failed_render_never_costs_the_post(conn, cfg, slot, when, tmp_path, monkeypatch):
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)

    class ImageHermes(MockHermes):
        def generate(self, request):
            r = super().generate(request)
            return HermesResponse(
                text=r.text, lane_id=r.lane_id, material_id=r.material_id,
                reasoning=r.reasoning, image_prompt="a prompt", image_alt="alt text",
            )

    monkeypatch.setattr(type(cfg), "images_allowed_for", lambda self, lane_id: True)
    pipeline.run_pull(conn, cfg)
    broken = MockImageRenderer(fail_with=ImageError("the model was busy"))
    outcome = pipeline.generate_for_slot(conn, cfg, ImageHermes(), slot, when, broken)

    row = db.get_post(conn, outcome.post_id)
    assert row["status"] == "pending", "the post survived"
    assert row["generated_text"], "with its text intact"
    assert row["image_path"] is None
    assert "the model was busy" in row["image_error"]
    assert "image_failed" in [e["kind"] for e in db.recent_events(conn)]


def test_an_approved_image_publishes_with_the_post(conn, cfg, slot, when, tmp_path,
                                                   monkeypatch):
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    monkeypatch.setattr(type(cfg), "images_allowed_for", lambda self, lane_id: True)

    class ImageHermes(MockHermes):
        def generate(self, request):
            r = super().generate(request)
            return HermesResponse(
                text=r.text, lane_id=r.lane_id, material_id=r.material_id,
                reasoning=r.reasoning, image_prompt="a prompt", image_alt="the alt text",
            )

    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(
        conn, cfg, ImageHermes(), slot, when, MockImageRenderer()
    )
    pipeline.approve(conn, outcome.post_id)

    from tests.test_pipeline import RecordingPublisher

    publisher = RecordingPublisher()
    pipeline.publish_due(conn, cfg, publisher, now=when)

    image_path, image_alt = publisher.images[0]
    assert image_path is not None and image_path.exists()
    assert image_alt == "the alt text"


def test_a_dropped_image_publishes_as_text(conn, cfg, slot, when, tmp_path, monkeypatch):
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    monkeypatch.setattr(type(cfg), "images_allowed_for", lambda self, lane_id: True)

    class ImageHermes(MockHermes):
        def generate(self, request):
            r = super().generate(request)
            return HermesResponse(
                text=r.text, lane_id=r.lane_id, material_id=r.material_id,
                reasoning=r.reasoning, image_prompt="a prompt", image_alt="alt",
            )

    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(
        conn, cfg, ImageHermes(), slot, when, MockImageRenderer()
    )
    db.drop_image(conn, outcome.post_id)
    pipeline.approve(conn, outcome.post_id)

    from tests.test_pipeline import RecordingPublisher

    publisher = RecordingPublisher()
    pipeline.publish_due(conn, cfg, publisher, now=when)

    assert publisher.published, "the post still went out"
    assert publisher.images[0] == (None, None)


def test_a_vanished_image_file_publishes_as_text(conn, cfg, slot, when, tmp_path,
                                                 monkeypatch):
    """Losing the picture is not worth losing the slot."""
    monkeypatch.setattr("multiagency.images.IMAGE_DIR", tmp_path)
    monkeypatch.setattr(type(cfg), "images_allowed_for", lambda self, lane_id: True)

    class ImageHermes(MockHermes):
        def generate(self, request):
            r = super().generate(request)
            return HermesResponse(
                text=r.text, lane_id=r.lane_id, material_id=r.material_id,
                reasoning=r.reasoning, image_prompt="a prompt", image_alt="alt",
            )

    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(
        conn, cfg, ImageHermes(), slot, when, MockImageRenderer()
    )
    db.get_post(conn, outcome.post_id)["image_path"]
    from pathlib import Path

    Path(db.get_post(conn, outcome.post_id)["image_path"]).unlink()
    pipeline.approve(conn, outcome.post_id)

    from tests.test_pipeline import RecordingPublisher

    publisher = RecordingPublisher()
    pipeline.publish_due(conn, cfg, publisher, now=when)

    assert publisher.published, "the post went out anyway"
    assert publisher.images[0] == (None, None)
    assert "image_missing_at_publish" in [e["kind"] for e in db.recent_events(conn)]


def test_no_renderer_configured_is_recorded_not_fatal(conn, cfg, slot, when, monkeypatch):
    monkeypatch.setattr(type(cfg), "images_allowed_for", lambda self, lane_id: True)

    class ImageHermes(MockHermes):
        def generate(self, request):
            r = super().generate(request)
            return HermesResponse(
                text=r.text, lane_id=r.lane_id, material_id=r.material_id,
                reasoning=r.reasoning, image_prompt="a prompt", image_alt="alt",
            )

    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(conn, cfg, ImageHermes(), slot, when, None)

    row = db.get_post(conn, outcome.post_id)
    assert row["status"] == "pending"
    assert "no image renderer" in row["image_error"]
