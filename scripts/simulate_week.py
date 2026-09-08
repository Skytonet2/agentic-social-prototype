"""Run a full week against a fake clock, a fake Hermes and a fake publisher.

This is the proof for the done-when list. It walks an hour at a time through
seven days, pulling, generating, reviewing and publishing, then reports:

* whether posts were generated and published on schedule
* how much of the reviewer's output needed an edit or a rejection
* whether the sources still hold unused material at the end of the week
* whether anything reached ``posted`` without a human approving it first

The reviewer here is a stand-in that approves most posts, edits some, rejects
one, and deliberately ignores one so an empty slot has to be handled.

    python scripts/simulate_week.py [--db data/week_sim.db] [--keep]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from multiagency import db, pipeline  # noqa: E402
from multiagency.clock import iso, local_str, parse  # noqa: E402
from multiagency.config import load_config  # noqa: E402
from multiagency.errors import PublishError  # noqa: E402
from multiagency.hermes import MockHermes  # noqa: E402
from multiagency.images import MockImageRenderer  # noqa: E402
from multiagency.publisher import MockPublisher  # noqa: E402

UTC = timezone.utc


class FlakyPublisher(MockPublisher):
    """Mock publisher that refuses one post, to exercise the failure path."""

    fail_on_call: int = 2

    def publish(self, text: str, *, image_path=None, image_alt=None) -> str:
        if self.counter + 1 == self.fail_on_call:
            self.counter += 1
            raise PublishError("X returned HTTP 429: rate limit exceeded")
        return super().publish(text, image_path=image_path, image_alt=image_alt)


class StandInReviewer:
    """A human, roughly. Approves most, edits some, rejects one, skips one."""

    def __init__(self) -> None:
        self.seen: set[int] = set()
        self.approved = 0
        self.edited = 0
        self.rejected = 0
        self.ignored = 0

    def review(self, conn, now: datetime) -> None:
        rows = conn.execute(
            "SELECT * FROM posts WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        for row in rows:
            post_id = int(row["id"])
            if post_id in self.seen:
                continue
            self.seen.add(post_id)
            n = len(self.seen)

            if n == 3:
                self.ignored += 1  # never looked at, so its slot must be skipped
                continue
            if n == 5:
                pipeline.reject(conn, post_id, "off topic for the lane")
                self.rejected += 1
                continue
            if n % 4 == 0:
                text = row["generated_text"].rstrip(".") + ". Worth saying plainly."
                pipeline.approve(conn, post_id, text[:240])
                self.edited += 1
                self.approved += 1
                continue
            pipeline.approve(conn, post_id)
            self.approved += 1


def check_approval_gate(conn) -> list[str]:
    """Every published post must carry a human approval before it went out."""
    problems: list[str] = []
    for row in conn.execute("SELECT * FROM posts WHERE status = 'posted'"):
        post_id = int(row["id"])
        if not row["reviewed_at"]:
            problems.append("post {} was published with no reviewed_at".format(post_id))
            continue
        approved_at = conn.execute(
            "SELECT at FROM events WHERE post_id = ? AND kind = 'approved' ORDER BY id LIMIT 1",
            (post_id,),
        ).fetchone()
        if approved_at is None:
            problems.append("post {} was published with no approval event".format(post_id))
        elif approved_at["at"] > (row["posted_at"] or ""):
            problems.append("post {} was approved after it was published".format(post_id))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/week_sim.db")
    parser.add_argument("--config", default="config/content.yaml")
    parser.add_argument("--keep", action="store_true", help="keep the database afterwards")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s",
    )

    for suffix in ("", "-wal", "-shm"):
        path = Path(args.db + suffix)
        if path.exists():
            path.unlink()

    cfg = load_config(args.config)
    conn = db.connect(args.db)
    db.sync_config(conn, cfg)

    hermes = MockHermes()
    renderer = MockImageRenderer()
    publisher = FlakyPublisher(path=Path(args.db).with_suffix(".published.log"))
    reviewer = StandInReviewer()

    # Start on a Monday at 06:00 so every slot in the schedule falls inside the run.
    start = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
    generated = 0
    skipped_slots: list[str] = []

    print("Simulating {} to {}\n".format(iso(start), iso(start + timedelta(days=7))))

    for hour in range(24 * 7):
        now = start + timedelta(hours=hour)

        if now.hour == 6:
            pipeline.run_pull(conn, cfg)
        if now.hour % 6 == 0:
            for outcome in pipeline.run_generation(
                conn, cfg, hermes, now=now, renderer=renderer
            ):
                if outcome.post_id:
                    generated += 1
                    print(
                        "  {}  generated post {} for {}{}".format(
                            local_str(now, cfg.timezone),
                            outcome.post_id,
                            outcome.slot_id,
                            " with an image" if outcome.image_path else "",
                        )
                    )

        # The reviewer looks at the queue twice a day.
        if now.hour in (9, 17):
            reviewer.review(conn, now)

        for post_id in pipeline.sweep_missed_slots(conn, cfg, now=now):
            row = db.get_post(conn, post_id)
            skipped_slots.append(row["slot_id"])
            print(
                "  {}  SLOT SKIPPED: {} had nothing approved".format(
                    local_str(now, cfg.timezone), row["slot_id"]
                )
            )

        for outcome in pipeline.publish_due(conn, cfg, publisher, now=now):
            print(
                "  {}  {}".format(
                    local_str(now, cfg.timezone),
                    "published post {} as {}".format(outcome.post_id, outcome.x_post_id)
                    if outcome.published
                    else "FAILED post {}: {}".format(outcome.post_id, outcome.failure_reason),
                )
            )

    counts = db.counts_by_status(conn)
    material = db.unused_material_counts(conn)
    reviewed = reviewer.approved + reviewer.rejected
    problems = check_approval_gate(conn)

    print("\n" + "=" * 66)
    print("A full week ran")
    print("  generated          {}".format(generated))
    print(
        "  posts              {}".format(
            ", ".join("{} {}".format(v, k) for k, v in counts.items() if v)
        )
    )
    print("\nApproval rate")
    if reviewed:
        print(
            "  approved as written {}/{}  ({:.0%})".format(
                reviewer.approved - reviewer.edited, reviewed,
                (reviewer.approved - reviewer.edited) / reviewed,
            )
        )
        print("  approved with edits {}/{}".format(reviewer.edited, reviewed))
        print("  rejected            {}/{}".format(reviewer.rejected, reviewed))
    print("  left unreviewed     {}".format(reviewer.ignored))

    print("\nMaterial left at the end of the week")
    exhausted = []
    for row in material:
        print(
            "  {:<14} {} unused of {} pulled".format(
                row["lane_id"], row["unused"] or 0, row["total"] or 0
            )
        )
        if not row["unused"]:
            exhausted.append(row["lane_id"])

    with_images = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE image_path IS NOT NULL"
    ).fetchone()[0]
    failed_images = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE image_error IS NOT NULL"
    ).fetchone()[0]
    print("\nImages")
    print(
        "  {} post(s) carried an image, {} render(s) failed".format(
            with_images, failed_images
        )
    )
    print("  only lanes with allow_images can get one")

    print("\nEmpty slots")
    if skipped_slots:
        for slot_id in skipped_slots:
            print("  {} skipped, nothing published to fill it".format(slot_id))
    else:
        print("  none")

    print("\nApproval gate")
    if problems:
        for problem in problems:
            print("  FAIL {}".format(problem))
    else:
        print(
            "  every one of the {} published post(s) was approved by a human first".format(
                counts["posted"]
            )
        )

    ok = not problems and counts["posted"] > 0 and not exhausted
    print("\n{}".format("PASS" if ok else "CHECK THE OUTPUT ABOVE"))
    print("=" * 66)

    conn.close()
    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            path = Path(args.db + suffix)
            if path.exists():
                path.unlink()
    else:
        print("\ndatabase kept at {}".format(args.db))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
