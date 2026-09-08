"""The flow: pull, generate, validate, queue, publish, skip.

Approval sits between queue and publish and is not in this module, because it
is a human action. There is no path from generate to publish that does not go
through a person changing a post to approved, and there is no flag that adds
one.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db
from .clock import iso, local_str, now_utc, occurrences_within, parse
from .config import ContentConfig, SlotConfig
from .errors import HermesError, PublishError
from .hermes import Hermes, MaterialBrief, PublishedPost, build_request
from .hermes.contract import RECENT_POST_WINDOW
from .publisher import Publisher
from .sources import pull_all
from .validation import retry_note, validate

log = logging.getLogger(__name__)

# How late a slot may be published if the process was down over its time.
# Beyond this the post is not sent, because a stale post is worse than none.
PUBLISH_GRACE_MINUTES = 120

# X's hard limit, checked on the human's own text before a doomed request.
PLATFORM_HARD_LIMIT = 280


@dataclass
class GenerationOutcome:
    slot_id: str
    lane_id: str
    scheduled_for: datetime
    post_id: int | None = None
    flagged: bool = False
    skipped: str | None = None
    error: str | None = None

    def __str__(self) -> str:
        when = iso(self.scheduled_for)
        if self.error:
            return "{} @ {}: generation failed ({})".format(self.slot_id, when, self.error)
        if self.skipped:
            return "{} @ {}: skipped ({})".format(self.slot_id, when, self.skipped)
        state = "flagged, queued for review" if self.flagged else "queued"
        return "{} @ {}: post {} {}".format(self.slot_id, when, self.post_id, state)


@dataclass
class PublishOutcome:
    post_id: int
    lane_id: str
    published: bool
    x_post_id: str | None = None
    failure_reason: str | None = None

    def __str__(self) -> str:
        if self.published:
            return "post {} published as {}".format(self.post_id, self.x_post_id)
        return "post {} failed: {}".format(self.post_id, self.failure_reason)


# --- 1. pull --------------------------------------------------------------


def run_pull(conn: sqlite3.Connection, cfg: ContentConfig):
    """Collect material from every configured source. Dedupe on fingerprint."""
    results = pull_all(conn, cfg)
    log.info(
        "pull complete: %d new item(s) across %d source(s)",
        sum(r.new for r in results),
        len(results),
    )
    return results


# --- 2 and 3. generate and validate ---------------------------------------


def _recent_published(conn: sqlite3.Connection) -> list[PublishedPost]:
    return [
        PublishedPost(lane_id=row["lane_id"], text=row["text"], posted_at=row["posted_at"])
        for row in db.recent_published(conn, RECENT_POST_WINDOW)
    ]


def generate_for_slot(
    conn: sqlite3.Connection,
    cfg: ContentConfig,
    hermes: Hermes,
    slot: SlotConfig,
    scheduled_for: datetime,
) -> GenerationOutcome:
    """Fill one slot occurrence with a pending post.

    Hermes writes it. This function selects the material, hands over the
    contract, checks the mechanical constraints, allows exactly one
    regeneration, and queues the result either way.
    """
    outcome = GenerationOutcome(slot.id, slot.lane_id, scheduled_for)

    if db.slot_is_filled(conn, slot.id, scheduled_for):
        outcome.skipped = "already has a post"
        return outcome

    row = db.next_unused_material(conn, slot.lane_id)
    if row is None:
        outcome.skipped = "no unused material for this lane"
        log.warning("%s", outcome)
        db.log_event(
            conn,
            "no_material",
            "slot {} at {} has no unused material".format(slot.id, iso(scheduled_for)),
            lane_id=slot.lane_id,
        )
        return outcome

    material = MaterialBrief(id=row["id"], source_id=row["source_id"], content=row["raw_content"])
    constraints = cfg.constraints_for(slot.lane_id)
    recent = _recent_published(conn)

    try:
        request = build_request(cfg, slot.lane_id, material, recent)
        response = hermes.generate(request)
        result = validate(response.text, constraints)

        if not result.ok:
            # Step 3: exactly one regeneration, then queue it flagged either way.
            log.info(
                "post for %s broke a constraint (%s), regenerating once",
                slot.id,
                result.summary,
            )
            db.log_event(
                conn,
                "regenerated",
                "slot {}: {}".format(slot.id, result.summary),
                lane_id=slot.lane_id,
            )
            retry_request = build_request(
                cfg, slot.lane_id, material, recent, retry_note=retry_note(result)
            )
            response = hermes.generate(retry_request)
            result = validate(response.text, constraints)
    except HermesError as exc:
        # Nothing to review, so nothing is queued. The material stays unused
        # and the failure is on the record rather than swallowed.
        outcome.error = str(exc)
        log.error("%s", outcome)
        db.log_event(
            conn,
            "generation_failed",
            "slot {}: {}".format(slot.id, exc),
            lane_id=slot.lane_id,
        )
        return outcome

    flagged = not result.ok
    flag_reason = result.summary if flagged else None
    if flagged:
        log.warning(
            "post for %s still breaks a constraint after regeneration (%s), "
            "queueing it flagged",
            slot.id,
            flag_reason,
        )

    with db.transaction(conn):
        post_id = db.insert_post(
            conn,
            lane_id=slot.lane_id,
            material_id=material.id,
            slot_id=slot.id,
            generated_text=response.text,
            reasoning=response.reasoning,
            scheduled_for=scheduled_for,
            flagged=flagged,
            flag_reason=flag_reason,
        )
        db.mark_material_used(conn, material.id)
        if flagged:
            db.log_event(
                conn,
                "post_flagged",
                flag_reason or "",
                lane_id=slot.lane_id,
                post_id=post_id,
            )

    outcome.post_id = post_id
    outcome.flagged = flagged
    log.info("%s", outcome)
    return outcome


def run_generation(
    conn: sqlite3.Connection,
    cfg: ContentConfig,
    hermes: Hermes,
    now: datetime | None = None,
) -> list[GenerationOutcome]:
    """Queue a pending post for every upcoming slot inside the lead window."""
    now = now or now_utc()
    outcomes: list[GenerationOutcome] = []
    for slot in cfg.active_slots:
        hour, minute = slot.hour_minute
        for when in occurrences_within(
            slot.weekday, hour, minute, cfg.timezone, now, cfg.generate_lead_hours
        ):
            outcomes.append(generate_for_slot(conn, cfg, hermes, slot, when))
    return outcomes


# --- 5 and 6. publish, or skip the slot -----------------------------------


def publish_due(
    conn: sqlite3.Connection,
    cfg: ContentConfig,
    publisher: Publisher,
    now: datetime | None = None,
) -> list[PublishOutcome]:
    """Publish approved posts whose slot has arrived.

    Only ``approved`` rows are ever considered. A pending post at its slot time
    is left alone and the slot is skipped.
    """
    now = now or now_utc()
    cutoff = iso(now)
    rows = conn.execute(
        """
        SELECT * FROM posts
        WHERE status = 'approved' AND scheduled_for IS NOT NULL AND scheduled_for <= ?
        ORDER BY scheduled_for ASC
        """,
        (cutoff,),
    ).fetchall()

    outcomes: list[PublishOutcome] = []
    for row in rows:
        outcomes.append(_publish_one(conn, row, publisher, now))
    return outcomes


def _publish_one(
    conn: sqlite3.Connection, row: sqlite3.Row, publisher: Publisher, now: datetime
) -> PublishOutcome:
    post_id = int(row["id"])
    scheduled_for = parse(row["scheduled_for"])
    text = db.final_text(row)

    late_by = now - scheduled_for if scheduled_for else timedelta(0)
    if late_by > timedelta(minutes=PUBLISH_GRACE_MINUTES):
        reason = (
            "slot passed {} minutes ago, beyond the {} minute grace window, so it "
            "was not published late".format(
                int(late_by.total_seconds() // 60), PUBLISH_GRACE_MINUTES
            )
        )
        return _fail(conn, post_id, row["lane_id"], reason)

    # A human may have edited the text. Their judgment on style stands; only
    # the platform's own hard limits are enforced on their version.
    if not text.strip():
        return _fail(conn, post_id, row["lane_id"], "the approved text is empty")
    if len(text) > PLATFORM_HARD_LIMIT:
        return _fail(
            conn,
            post_id,
            row["lane_id"],
            "the approved text is {} characters, over the {} character platform "
            "limit".format(len(text), PLATFORM_HARD_LIMIT),
        )

    try:
        x_post_id = publisher.publish(text)
    except PublishError as exc:
        # No silent retry. A human decides whether this goes out again.
        return _fail(conn, post_id, row["lane_id"], str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected error is still a failure
        return _fail(conn, post_id, row["lane_id"], "{}: {}".format(type(exc).__name__, exc))

    db.set_status(
        conn,
        post_id,
        "posted",
        posted_at=iso(now),
        x_post_id=x_post_id,
        failure_reason=None,
    )
    db.log_event(conn, "posted", x_post_id, lane_id=row["lane_id"], post_id=post_id)
    outcome = PublishOutcome(post_id, row["lane_id"], True, x_post_id=x_post_id)
    log.info("%s", outcome)
    return outcome


def _fail(
    conn: sqlite3.Connection, post_id: int, lane_id: str, reason: str
) -> PublishOutcome:
    db.set_status(conn, post_id, "failed", failure_reason=reason)
    db.log_event(conn, "publish_failed", reason, lane_id=lane_id, post_id=post_id)
    outcome = PublishOutcome(post_id, lane_id, False, failure_reason=reason)
    log.error("%s", outcome)
    return outcome


def sweep_missed_slots(
    conn: sqlite3.Connection, cfg: ContentConfig, now: datetime | None = None
) -> list[int]:
    """Record slots that arrived with nothing approved.

    The post stays pending so a reviewer can still deal with it. Nothing is
    published to fill the gap.
    """
    now = now or now_utc()
    rows = conn.execute(
        """
        SELECT p.id, p.lane_id, p.slot_id, p.scheduled_for FROM posts p
        WHERE p.status = 'pending'
          AND p.scheduled_for IS NOT NULL
          AND p.scheduled_for <= ?
          AND NOT EXISTS (
              SELECT 1 FROM events e WHERE e.post_id = p.id AND e.kind = 'slot_skipped'
          )
        """,
        (iso(now),),
    ).fetchall()

    skipped: list[int] = []
    for row in rows:
        when = local_str(parse(row["scheduled_for"]), cfg.timezone)
        detail = "slot {} at {} arrived with nothing approved, so it was skipped".format(
            row["slot_id"], when
        )
        log.warning("%s", detail)
        db.log_event(
            conn, "slot_skipped", detail, lane_id=row["lane_id"], post_id=row["id"]
        )
        skipped.append(int(row["id"]))
    return skipped


# --- 4. approval (human) --------------------------------------------------


def approve(
    conn: sqlite3.Connection, post_id: int, edited_text: str | None = None
) -> None:
    """Mark a post approved. The only route to publication."""
    fields = {"reviewed_at": iso(now_utc())}
    if edited_text is not None:
        fields["edited_text"] = edited_text.strip()
    db.set_status(conn, post_id, "approved", **fields)
    db.log_event(conn, "approved", "", post_id=post_id)


def reject(conn: sqlite3.Connection, post_id: int, note: str = "") -> None:
    db.set_status(conn, post_id, "rejected", reviewed_at=iso(now_utc()))
    db.log_event(conn, "rejected", note, post_id=post_id)


def save_edit(conn: sqlite3.Connection, post_id: int, edited_text: str) -> None:
    """Store an edit without approving. The post stays pending."""
    conn.execute(
        "UPDATE posts SET edited_text = ? WHERE id = ?", (edited_text.strip(), post_id)
    )
    db.log_event(conn, "edited", "", post_id=post_id)
