"""The Hermes contract.

Hermes is the reasoning layer. It decides whether the material is worth posting
and it writes the post. Nothing in this package generates text, rewrites text,
chains prompts, or calls a second model. If the output is wrong, the fix is the
lane definition or the constraints block in the YAML, not code here.

What Hermes receives
    lane            audience, post type, purpose, worked example
    constraints     the lane block folded onto the global block
    material        the candidate source material
    recent_posts    the last 10 posts published, so it does not repeat itself

What Hermes returns
    text            the post
    lane_id         which lane it is for
    material_id     which material it drew from
    reasoning       one or two sentences on why this was worth posting
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Constraints, ContentConfig, LaneConfig
from ..errors import HermesError

RECENT_POST_WINDOW = 10


@dataclass(frozen=True)
class LaneBrief:
    id: str
    audience: str
    post_type: str
    purpose: str
    example: str

    @classmethod
    def from_config(cls, lane: LaneConfig) -> "LaneBrief":
        return cls(
            id=lane.id,
            audience=lane.audience,
            post_type=lane.post_type,
            purpose=lane.purpose,
            example=lane.example,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "audience": self.audience,
            "post_type": self.post_type,
            "purpose": self.purpose,
            "example": self.example,
        }


@dataclass(frozen=True)
class MaterialBrief:
    id: int
    source_id: str
    content: str

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "source_id": self.source_id, "content": self.content}


@dataclass(frozen=True)
class PublishedPost:
    lane_id: str
    text: str
    posted_at: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"lane_id": self.lane_id, "text": self.text, "posted_at": self.posted_at}


@dataclass(frozen=True)
class HermesRequest:
    lane: LaneBrief
    constraints: Constraints
    material: MaterialBrief
    recent_posts: tuple[PublishedPost, ...] = ()
    # Set only on the single retry allowed by step 3 of the flow. It states
    # which mechanical constraint the previous attempt broke. It is not a
    # rewrite instruction and it is not chaining: Hermes generates afresh.
    retry_note: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "lane": self.lane.to_payload(),
            "constraints": {
                "max_length": self.constraints.max_length,
                "allow_emojis": self.constraints.allow_emojis,
                "forbid_em_dash": self.constraints.forbid_em_dash,
                "rules": list(self.constraints.rules),
            },
            "material": self.material.to_payload(),
            "recent_posts": [p.to_payload() for p in self.recent_posts],
            "retry_note": self.retry_note,
        }


@dataclass(frozen=True)
class HermesResponse:
    text: str
    lane_id: str
    material_id: int
    reasoning: str

    @classmethod
    def from_payload(cls, payload: Any, request: HermesRequest) -> "HermesResponse":
        """Parse and check a response against the request that produced it.

        A response that does not match the contract is an error, not something
        to paper over. The pipeline turns it into a visible failure.
        """
        if not isinstance(payload, dict):
            raise HermesError(
                "expected a JSON object from Hermes, got {}".format(type(payload).__name__)
            )

        missing = [k for k in ("text", "lane_id", "material_id", "reasoning") if k not in payload]
        if missing:
            raise HermesError("Hermes response is missing {}".format(", ".join(missing)))

        text = payload["text"]
        reasoning = payload["reasoning"]
        if not isinstance(text, str) or not text.strip():
            raise HermesError("Hermes returned an empty post")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise HermesError(
                "Hermes returned no reasoning. The reviewer needs it to judge "
                "whether the pick was right, so an empty value is a failure."
            )
        if payload["lane_id"] != request.lane.id:
            raise HermesError(
                "Hermes returned lane_id {!r}, asked for {!r}".format(
                    payload["lane_id"], request.lane.id
                )
            )
        try:
            material_id = int(payload["material_id"])
        except (TypeError, ValueError) as exc:
            raise HermesError(
                "Hermes returned a non-numeric material_id {!r}".format(payload["material_id"])
            ) from exc
        if material_id != request.material.id:
            raise HermesError(
                "Hermes returned material_id {}, was given {}".format(
                    material_id, request.material.id
                )
            )

        return cls(
            text=text.strip(),
            lane_id=payload["lane_id"],
            material_id=material_id,
            reasoning=" ".join(reasoning.split()),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "lane_id": self.lane_id,
            "material_id": self.material_id,
            "reasoning": self.reasoning,
        }


def build_request(
    cfg: ContentConfig,
    lane_id: str,
    material: MaterialBrief,
    recent_posts: list[PublishedPost] | tuple[PublishedPost, ...] = (),
    retry_note: str | None = None,
) -> HermesRequest:
    """Assemble everything Hermes is owed for one generation."""
    return HermesRequest(
        lane=LaneBrief.from_config(cfg.lane(lane_id)),
        constraints=cfg.constraints_for(lane_id),
        material=material,
        recent_posts=tuple(recent_posts)[:RECENT_POST_WINDOW],
        retry_note=retry_note,
    )
