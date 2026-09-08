"""One live round trip against Hermes, reported in full.

This is the tool for the first day of a real connection. It builds a genuine
request from real material, sends it, and shows exactly what came back and
which parts of the contract are satisfied.

It writes nothing. No post is queued, no material is marked used. A failure
here costs nothing but the call.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .config import ContentConfig
from .db import next_unused_material, recent_published
from .errors import HermesError
from .hermes import HermesRequest, HermesResponse, MaterialBrief, PublishedPost, build_request
from .hermes.adapter import HttpHermes
from .hermes.contract import RECENT_POST_WINDOW
from .validation import validate

# Used when the database holds no material yet, so the probe works on a
# freshly cloned repo before anything has been pulled.
PLACEHOLDER_MATERIAL = MaterialBrief(
    id=0,
    source_id="probe",
    content=(
        "Agents that retry silently are worse than agents that fail loudly. "
        "We lost two days to a retry loop swallowing a 401."
    ),
)


@dataclass
class ProbeResult:
    request: HermesRequest
    raw_response: object | None = None
    response: HermesResponse | None = None
    contract_error: str | None = None
    transport_error: str | None = None
    constraint_summary: str | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None and not self.contract_error


def build_probe_request(
    conn: sqlite3.Connection, cfg: ContentConfig, lane_id: str
) -> HermesRequest:
    """A real request: real lane, real constraints, real material, real history."""
    row = next_unused_material(conn, lane_id)
    material = (
        MaterialBrief(id=row["id"], source_id=row["source_id"], content=row["raw_content"])
        if row is not None
        else PLACEHOLDER_MATERIAL
    )
    recent = [
        PublishedPost(lane_id=r["lane_id"], text=r["text"], posted_at=r["posted_at"])
        for r in recent_published(conn, RECENT_POST_WINDOW)
    ]
    return build_request(cfg, lane_id, material, recent)


def run_probe(
    conn: sqlite3.Connection, cfg: ContentConfig, hermes: HttpHermes, lane_id: str
) -> ProbeResult:
    """Send one request and check the answer. Never writes to the database."""
    request = build_probe_request(conn, cfg, lane_id)
    result = ProbeResult(request=request)

    try:
        result.raw_response = hermes.call_raw(request)
    except HermesError as exc:
        result.transport_error = str(exc)
        return result

    try:
        result.response = HermesResponse.from_payload(result.raw_response, request)
    except HermesError as exc:
        result.contract_error = str(exc)
        return result

    checked = validate(result.response.text, request.constraints)
    result.constraint_summary = None if checked.ok else checked.summary
    return result


def render(result: ProbeResult, *, show_payload: bool) -> str:
    """A report an operator can read, and paste to whoever owns Hermes."""
    lines: list[str] = []
    request = result.request

    lines.append("Lane      {} ({}, {})".format(
        request.lane.id, request.lane.audience, request.lane.post_type
    ))
    lines.append("Material  id {} from {}".format(
        request.material.id, request.material.source_id
    ))
    lines.append("Sending   {} constraint rule(s), {} recent published post(s)".format(
        len(request.constraints.rules), len(request.recent_posts)
    ))

    if show_payload:
        lines.append("")
        lines.append("--- request payload ---")
        lines.append(json.dumps(request.to_payload(), indent=2))

    if result.transport_error:
        lines.append("")
        lines.append("FAILED before Hermes answered")
        lines.append("  {}".format(result.transport_error))
        return "\n".join(lines)

    lines.append("")
    lines.append("--- response ---")
    lines.append(json.dumps(result.raw_response, indent=2, default=str)[:4000])

    lines.append("")
    lines.append("--- contract ---")
    if result.contract_error:
        lines.append("  FAILED  {}".format(result.contract_error))
        lines.append("")
        lines.append(
            "  Hermes answered, but not in the shape this system expects. Either\n"
            "  the endpoint is wrong, or the response needs mapping. The one\n"
            "  place to map it is HttpHermes.call_raw in multiagency/hermes/adapter.py."
        )
        return "\n".join(lines)

    response = result.response
    for label, value in (
        ("text", response.text),
        ("lane_id", response.lane_id),
        ("material_id", response.material_id),
        ("reasoning", response.reasoning),
    ):
        lines.append("  ok  {:<12} {}".format(label, _clip(value)))

    lines.append("")
    lines.append("--- constraints ---")
    if result.constraint_summary:
        lines.append("  the post {}".format(result.constraint_summary))
        lines.append(
            "  In a real run this would trigger one regeneration, then be queued\n"
            "  flagged if it happened again. Nothing is ever dropped."
        )
    else:
        lines.append("  ok  {} characters, limit {}".format(
            len(response.text), request.constraints.max_length
        ))

    lines.append("")
    lines.append("Nothing was written. No post was queued and no material was used.")
    return "\n".join(lines)


def _clip(value: object, limit: int = 90) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "..."
