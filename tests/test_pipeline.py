"""The flow, with the approval gate as the test that matters most."""

from __future__ import annotations

from datetime import timedelta

import pytest

from multiagency import db, pipeline
from multiagency.errors import HermesError, PublishError
from multiagency.hermes import MockHermes
from multiagency.publisher import MockPublisher


class RecordingPublisher:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.published: list[str] = []
        self.fail_with = fail_with

    def publish(self, text: str) -> str:
        if self.fail_with:
            raise self.fail_with
        self.published.append(text)
        return "x-{}".format(len(self.published))


# --- 1. pull --------------------------------------------------------------


def test_pull_collects_material(conn, cfg):
    results = pipeline.run_pull(conn, cfg)
    assert results[0].new == 4
    assert conn.execute("SELECT COUNT(*) FROM material").fetchone()[0] == 4


def test_pull_dedupes_on_fingerprint(conn, cfg, notes_file):
    pipeline.run_pull(conn, cfg)
    # The same lines again, one of them reworded only in spacing and case.
    lines = notes_file.read_text(encoding="utf-8").splitlines()
    notes_file.write_text(
        "\n".join(lines + ["  " + lines[1].upper() + "  ", "A genuinely new note about slots."]),
        encoding="utf-8",
    )
    results = pipeline.run_pull(conn, cfg)
    assert results[0].new == 1, "only the genuinely new line is stored"
    assert results[0].duplicates == 5


def test_a_broken_source_does_not_stop_the_run(conn, cfg, notes_file):
    notes_file.unlink()
    results = pipeline.run_pull(conn, cfg)
    assert results[0].error and "FileNotFoundError" in results[0].error
    kinds = [e["kind"] for e in db.recent_events(conn)]
    assert "source_failed" in kinds


# --- 2 and 3. generate and validate ---------------------------------------


def test_generation_queues_a_pending_post(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)

    row = db.get_post(conn, outcome.post_id)
    assert row["status"] == "pending"
    assert row["reasoning"], "reasoning is stored for the reviewer"
    assert row["material_id"] is not None
    assert row["scheduled_for"] == "2026-09-15T10:00:00Z"


def test_material_is_marked_used_and_not_reused(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    first = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    second = pipeline.generate_for_slot(
        conn, cfg, MockHermes(), slot, when + timedelta(days=7)
    )
    used = {
        db.get_post(conn, first.post_id)["material_id"],
        db.get_post(conn, second.post_id)["material_id"],
    }
    assert len(used) == 2


def test_a_slot_is_only_filled_once(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    again = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    assert again.post_id is None
    assert again.skipped == "already has a post"


def test_a_rejected_post_frees_its_slot(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    first = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    pipeline.reject(conn, first.post_id)
    second = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    assert second.post_id is not None and second.post_id != first.post_id


def test_one_violation_triggers_exactly_one_regeneration(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    hermes = MockHermes(violations=["em_dash"])
    outcome = pipeline.generate_for_slot(conn, cfg, hermes, slot, when)

    assert len(hermes.calls) == 2, "one retry, not a loop"
    assert hermes.calls[1].retry_note and "em dash" in hermes.calls[1].retry_note
    row = db.get_post(conn, outcome.post_id)
    assert row["flagged"] == 0


def test_two_violations_queue_the_post_flagged_rather_than_dropping_it(
    conn, cfg, slot, when
):
    pipeline.run_pull(conn, cfg)
    hermes = MockHermes(violations=["em_dash", "emoji"])
    outcome = pipeline.generate_for_slot(conn, cfg, hermes, slot, when)

    assert len(hermes.calls) == 2
    row = db.get_post(conn, outcome.post_id)
    assert row["status"] == "pending", "it is queued, never dropped"
    assert row["flagged"] == 1
    assert "emoji" in row["flag_reason"]


def test_no_material_skips_the_slot_and_logs_it(conn, cfg, slot, when):
    outcome = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    assert outcome.post_id is None
    assert "no unused material" in outcome.skipped
    assert "no_material" in [e["kind"] for e in db.recent_events(conn)]


def test_a_hermes_failure_queues_nothing_and_keeps_the_material(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    before = db.next_unused_material(conn, "field_notes")["id"]
    hermes = MockHermes(fail_with=HermesError("endpoint refused the connection"))

    outcome = pipeline.generate_for_slot(conn, cfg, hermes, slot, when)

    assert outcome.post_id is None and "refused" in outcome.error
    assert db.next_unused_material(conn, "field_notes")["id"] == before
    assert "generation_failed" in [e["kind"] for e in db.recent_events(conn)]


def test_generation_sends_the_last_published_posts(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    first = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    pipeline.approve(conn, first.post_id)
    pipeline.publish_due(conn, cfg, MockPublisher(), now=when)

    hermes = MockHermes()
    pipeline.generate_for_slot(conn, cfg, hermes, slot, when + timedelta(days=7))
    recent = hermes.calls[0].recent_posts
    assert len(recent) == 1 and recent[0].lane_id == "field_notes"


def test_run_generation_only_fills_slots_inside_the_lead_window(conn, cfg, when):
    pipeline.run_pull(conn, cfg)
    # 36 hours before the Tuesday slot, so exactly one occurrence is in range.
    outcomes = pipeline.run_generation(
        conn, cfg, MockHermes(), now=when - timedelta(hours=20)
    )
    assert len([o for o in outcomes if o.post_id]) == 1


# --- 4 and 5. approval gate and publishing --------------------------------


def test_pending_posts_are_never_published(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    publisher = RecordingPublisher()

    outcomes = pipeline.publish_due(conn, cfg, publisher, now=when + timedelta(minutes=1))

    assert outcomes == []
    assert publisher.published == []


def test_flagged_posts_are_never_published_without_approval(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    pipeline.generate_for_slot(
        conn, cfg, MockHermes(violations=["em_dash", "em_dash"]), slot, when
    )
    publisher = RecordingPublisher()
    pipeline.publish_due(conn, cfg, publisher, now=when + timedelta(hours=1))
    assert publisher.published == []


def test_approved_posts_publish_at_their_slot(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    pipeline.approve(conn, outcome.post_id)
    publisher = RecordingPublisher()

    before = pipeline.publish_due(conn, cfg, publisher, now=when - timedelta(minutes=5))
    assert before == [], "not before the slot"

    after = pipeline.publish_due(conn, cfg, publisher, now=when)
    row = db.get_post(conn, outcome.post_id)
    assert after[0].published
    assert row["status"] == "posted"
    assert row["x_post_id"] == "x-1"
    assert row["posted_at"] == "2026-09-15T10:00:00Z"


def test_the_edited_text_is_what_publishes(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    pipeline.approve(conn, outcome.post_id, "The reviewer's own words.")
    publisher = RecordingPublisher()

    pipeline.publish_due(conn, cfg, publisher, now=when)

    assert publisher.published == ["The reviewer's own words."]
    row = db.get_post(conn, outcome.post_id)
    assert row["generated_text"] != row["edited_text"], "the draft is kept"


def test_a_publish_failure_is_recorded_and_not_retried(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    pipeline.approve(conn, outcome.post_id)
    publisher = RecordingPublisher(fail_with=PublishError("HTTP 429: rate limit"))

    pipeline.publish_due(conn, cfg, publisher, now=when)
    row = db.get_post(conn, outcome.post_id)
    assert row["status"] == "failed"
    assert "429" in row["failure_reason"]

    # A second pass must not pick it up again.
    assert pipeline.publish_due(conn, cfg, publisher, now=when + timedelta(minutes=5)) == []


def test_a_post_is_not_published_long_after_its_slot(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)
    pipeline.approve(conn, outcome.post_id)
    publisher = RecordingPublisher()

    pipeline.publish_due(conn, cfg, publisher, now=when + timedelta(hours=6))

    assert publisher.published == []
    row = db.get_post(conn, outcome.post_id)
    assert row["status"] == "failed" and "grace window" in row["failure_reason"]


# --- 6. empty slot --------------------------------------------------------


def test_an_empty_slot_is_skipped_and_logged_once(conn, cfg, slot, when):
    pipeline.run_pull(conn, cfg)
    outcome = pipeline.generate_for_slot(conn, cfg, MockHermes(), slot, when)

    skipped = pipeline.sweep_missed_slots(conn, cfg, now=when + timedelta(minutes=1))
    assert skipped == [outcome.post_id]

    again = pipeline.sweep_missed_slots(conn, cfg, now=when + timedelta(minutes=30))
    assert again == [], "the same slot is not logged twice"

    row = db.get_post(conn, outcome.post_id)
    assert row["status"] == "pending", "a skipped slot leaves the post for review"
    events = [e for e in db.recent_events(conn) if e["kind"] == "slot_skipped"]
    assert len(events) == 1 and "nothing approved" in events[0]["detail"]
