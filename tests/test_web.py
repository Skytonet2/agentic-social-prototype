"""The approval page. The gate lives here, so the gate is what is tested."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from multiagency import db, pipeline
from multiagency.hermes import MockHermes
from multiagency.settings import Settings
from multiagency.web import create_app


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        config_path=tmp_path / "content.yaml",
        db_path=tmp_path / "test.db",
        hermes_mode="mock",
        hermes_endpoint="",
        hermes_api_key="",
        publisher="mock",
        ui_username="",
        ui_password="",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def queued(conn, cfg, slot, when):
    """One pending post, one flagged pending post."""
    pipeline.run_pull(conn, cfg)
    clean = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    flagged = pipeline.generate_for_slot(
        conn,
        cfg,
        MockHermes(violations=["em_dash", "em_dash"]),
        slot,
        when + timedelta(days=7),
    )
    return clean.post_id, flagged.post_id


@pytest.fixture
def client(conn, cfg, tmp_path):
    app = create_app(conn, cfg, make_settings(tmp_path))
    return TestClient(app)


def test_the_queue_shows_what_a_reviewer_needs(client, conn, queued):
    clean_id, _ = queued
    body = client.get("/").text
    row = db.get_post(conn, clean_id)

    assert "Why Hermes picked this" in body
    assert row["reasoning"][:40] in body, "the reasoning is on the page"
    assert "Source material" in body
    assert row["material_content"][:40] in body
    assert 'data-limit="200"' in body, "the character limit for the lane"
    assert ">Approve<" in body and ">Reject<" in body


def test_flagged_posts_are_visually_distinct(client, queued):
    body = client.get("/").text
    assert 'class="card flagged"' in body
    assert "Flagged: this still contains an em dash" in body


def test_approve_moves_the_post_and_records_the_review(client, conn, queued):
    clean_id, _ = queued
    client.post("/posts/{}/approve".format(clean_id), data={"text": "unchanged"})
    row = db.get_post(conn, clean_id)
    assert row["status"] == "approved"
    assert row["reviewed_at"]


def test_approving_the_draft_unchanged_stores_no_edit(client, conn, queued):
    clean_id, _ = queued
    original = db.get_post(conn, clean_id)["generated_text"]
    client.post("/posts/{}/approve".format(clean_id), data={"text": original})
    assert db.get_post(conn, clean_id)["edited_text"] is None


def test_an_edit_is_stored_separately_from_the_draft(client, conn, queued):
    clean_id, _ = queued
    client.post("/posts/{}/approve".format(clean_id), data={"text": "My own words."})
    row = db.get_post(conn, clean_id)
    assert row["edited_text"] == "My own words."
    assert row["generated_text"] != "My own words."


def test_save_edit_keeps_the_post_pending(client, conn, queued):
    clean_id, _ = queued
    client.post("/posts/{}/edit".format(clean_id), data={"text": "Halfway there."})
    row = db.get_post(conn, clean_id)
    assert row["status"] == "pending" and row["edited_text"] == "Halfway there."


def test_an_over_limit_edit_is_refused_but_kept(client, conn, queued):
    clean_id, _ = queued
    long_text = "x" * 300
    response = client.post(
        "/posts/{}/approve".format(clean_id), data={"text": long_text}, follow_redirects=False
    )
    row = db.get_post(conn, clean_id)
    assert row["status"] == "pending", "not approved"
    assert row["edited_text"] == long_text, "the work is not thrown away"
    assert "over%20the%20280" in response.headers["location"]


def test_empty_text_cannot_be_approved(client, conn, queued):
    clean_id, _ = queued
    client.post("/posts/{}/approve".format(clean_id), data={"text": "   "})
    assert db.get_post(conn, clean_id)["status"] == "pending"


def test_reject(client, conn, queued):
    clean_id, _ = queued
    client.post("/posts/{}/reject".format(clean_id))
    assert db.get_post(conn, clean_id)["status"] == "rejected"


def test_a_published_post_cannot_be_pulled_back(client, conn, cfg, queued, when):
    from multiagency.publisher import MockPublisher

    clean_id, _ = queued
    pipeline.approve(conn, clean_id)
    pipeline.publish_due(conn, cfg, MockPublisher(), now=when)
    client.post("/posts/{}/unapprove".format(clean_id))
    assert db.get_post(conn, clean_id)["status"] == "posted"


def test_no_route_can_publish(client, conn, queued):
    """There is no auto-post path, not even behind a flag."""
    clean_id, flagged_id = queued
    for post_id in (clean_id, flagged_id):
        for path in ("approve", "edit", "reject", "unapprove"):
            client.post(
                "/posts/{}/{}".format(post_id, path), data={"text": "some text"}
            )
    statuses = {row["status"] for row in conn.execute("SELECT status FROM posts")}
    assert "posted" not in statuses


def test_unknown_post_is_a_404(client):
    assert client.post("/posts/9999/approve", data={"text": "x"}).status_code == 404


def test_basic_auth_is_enforced_when_configured(conn, cfg, tmp_path):
    settings = make_settings(tmp_path, ui_username="editor", ui_password="secret")
    guarded = TestClient(create_app(conn, cfg, settings))

    assert guarded.get("/").status_code == 401
    assert guarded.get("/", auth=("editor", "wrong")).status_code == 401
    assert guarded.get("/", auth=("editor", "secret")).status_code == 200


def test_html_is_escaped(conn, cfg, tmp_path, slot, when):
    """Material comes from outside, so it is data, never markup."""
    conn.execute(
        "INSERT INTO material (source_id, raw_content, pulled_at, used, fingerprint) "
        "VALUES ('notes_file', ?, '2026-09-01T00:00:00Z', 0, 'fp1')",
        ("<script>alert('x')</script> a note about escaping",),
    )
    pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    client = TestClient(create_app(conn, cfg, make_settings(tmp_path)))

    body = client.get("/").text
    assert "<script>alert(" not in body
    assert "&lt;script&gt;" in body
