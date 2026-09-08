"""A fake Hermes, for tests and for running the pipeline end to end offline.

This is a stand-in, not a writer. It reshapes the material into something the
right length and reports why it picked it, so the plumbing around Hermes can be
exercised without a live call. It can also be told to break a hard constraint,
which is how the regenerate-once path in step 3 gets tested.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Iterable

from .contract import HermesRequest, HermesResponse

EM_DASH = "—"

# What a caller can ask the mock to get wrong, in order.
VIOLATIONS = ("em_dash", "emoji", "too_long")


class MockHermes:
    """Deterministic fake.

    ``violations`` is consumed one entry per call, so ``["em_dash"]`` breaks the
    first attempt and lets the retry succeed, while ``["em_dash", "em_dash"]``
    breaks both and exercises the flagged path.
    """

    def __init__(
        self,
        violations: Iterable[str] | None = None,
        *,
        fail_with: Exception | None = None,
    ) -> None:
        self.violations: deque[str] = deque(violations or ())
        self.fail_with = fail_with
        self.calls: list[HermesRequest] = []

    def generate(self, request: HermesRequest) -> HermesResponse:
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with

        text = self._compose(request)
        violation = self.violations.popleft() if self.violations else None
        if violation:
            text = self._break(text, violation, request.constraints.max_length)

        return HermesResponse(
            text=text,
            lane_id=request.lane.id,
            material_id=request.material.id,
            reasoning=self._reasoning(request),
        )

    # -- composition -------------------------------------------------------

    def _compose(self, request: HermesRequest) -> str:
        body = " ".join(request.material.content.split())
        body = body.replace(EM_DASH, ", ").replace("–", "-")

        # A crude nod to the recent-post window: if this lane has published
        # something with the same opening recently, shift the framing so the
        # fake does not look like it is looping either.
        seen_openings = {
            " ".join(p.text.split()[:4]).lower()
            for p in request.recent_posts
            if p.lane_id == request.lane.id
        }
        if " ".join(body.split()[:4]).lower() in seen_openings:
            body = "One more from the same week. " + body

        return _truncate(body, request.constraints.max_length)

    def _reasoning(self, request: HermesRequest) -> str:
        return (
            "Picked {} from {} because it is the oldest unused item in the {} lane "
            "and reads as {} for {}.".format(
                request.material.id,
                request.material.source_id,
                request.lane.id,
                _article(request.lane.post_type.replace("_", " ")),
                request.lane.audience,
            )
        )

    def _break(self, text: str, kind: str, max_length: int) -> str:
        if kind == "em_dash":
            head, _, tail = text.partition(" ")
            return "{}{}{}".format(head, EM_DASH, tail or "and that is the point")
        if kind == "emoji":
            return _truncate(text, max_length - 2) + " \U0001f680"
        if kind == "too_long":
            filler = " and there is more to say about it besides"
            while len(text) <= max_length:
                text += filler
            return text
        raise ValueError("unknown violation {!r}, expected one of {}".format(kind, VIOLATIONS))


def _article(noun: str) -> str:
    return "{} {}".format("an" if noun[:1].lower() in "aeiou" else "a", noun)


def _truncate(text: str, limit: int) -> str:
    """Cut at a word boundary, then at a sentence boundary when one is close."""
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    cut = cut[: cut.rfind(" ")] if " " in cut else cut[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if sentence_end > limit * 0.6:
        cut = cut[: sentence_end + 1]
    return re.sub(r"[\s,;:]+$", "", cut)
