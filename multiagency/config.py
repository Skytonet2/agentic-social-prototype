"""Load and validate the content system YAML.

Lanes, sources, constraints and the schedule all live in one file. The person
tuning the content system is not the person maintaining this codebase, so the
validation here is deliberately noisy: every problem in the file is collected
and reported at once, with the path to the offending key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

AUDIENCES = {"everyone", "builders", "projects", "clients"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SOURCE_TYPES = {"file_lines", "directory", "jsonl", "rss"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# X's own limit. A lane may go lower, never higher.
PLATFORM_MAX_LENGTH = 280


# X's own cap on alt text.
ALT_TEXT_MAX = 1000
IMAGE_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
IMAGE_QUALITIES = {"low", "medium", "high", "auto"}


@dataclass(frozen=True)
class ImageSettings:
    """How images get made, when a lane asks for one.

    ``enabled`` is the master switch. A lane still has to opt in with
    ``allow_images``, so turning this on does not put pictures on everything.
    """

    enabled: bool
    model: str
    size: str
    quality: str
    alt_text_max: int = ALT_TEXT_MAX


@dataclass(frozen=True)
class Constraints:
    """The constraint block handed to Hermes and checked by the validator.

    ``max_length``, ``allow_emojis`` and ``forbid_em_dash`` are mechanical and
    checked in code. ``rules`` are judgment calls and are Hermes's job.
    """

    max_length: int
    allow_emojis: bool
    forbid_em_dash: bool
    rules: tuple[str, ...] = ()

    def merged_with(self, lane: "Constraints | None") -> "Constraints":
        """Fold a lane constraint block on top of the global one.

        A lane may only tighten the mechanical limits. It may enable emojis,
        because the global rule is "no emojis unless the lane explicitly
        allows them". It may not re-enable em-dashes.
        """
        if lane is None:
            return self
        return Constraints(
            max_length=min(self.max_length, lane.max_length),
            allow_emojis=lane.allow_emojis,
            forbid_em_dash=self.forbid_em_dash or lane.forbid_em_dash,
            rules=tuple(self.rules) + tuple(lane.rules),
        )


@dataclass(frozen=True)
class SourceConfig:
    id: str
    lane_id: str
    source_type: str
    location: str


@dataclass(frozen=True)
class LaneConfig:
    id: str
    audience: str
    post_type: str
    purpose: str
    example: str
    constraints: Constraints | None
    active: bool
    allow_images: bool = False
    sources: tuple[SourceConfig, ...] = ()


@dataclass(frozen=True)
class SlotConfig:
    id: str
    lane_id: str
    day_of_week: str
    time: str
    active: bool

    @property
    def weekday(self) -> int:
        """0 = Monday, matching datetime.weekday()."""
        return DAYS.index(self.day_of_week)

    @property
    def hour_minute(self) -> tuple[int, int]:
        hour, minute = self.time.split(":")
        return int(hour), int(minute)


@dataclass(frozen=True)
class ContentConfig:
    timezone: str
    pull_interval_minutes: int
    generate_lead_hours: int
    global_constraints: Constraints
    lanes: tuple[LaneConfig, ...]
    schedule: tuple[SlotConfig, ...]
    images: ImageSettings
    path: Path | None = None

    def images_allowed_for(self, lane_id: str) -> bool:
        """A lane gets an image only if the system and the lane both say so."""
        return self.images.enabled and self.lane(lane_id).allow_images

    def lane(self, lane_id: str) -> LaneConfig:
        for lane in self.lanes:
            if lane.id == lane_id:
                return lane
        raise KeyError(lane_id)

    def constraints_for(self, lane_id: str) -> Constraints:
        return self.global_constraints.merged_with(self.lane(lane_id).constraints)

    @property
    def sources(self) -> tuple[SourceConfig, ...]:
        return tuple(s for lane in self.lanes for s in lane.sources)

    @property
    def active_slots(self) -> tuple[SlotConfig, ...]:
        active_lanes = {lane.id for lane in self.lanes if lane.active}
        return tuple(s for s in self.schedule if s.active and s.lane_id in active_lanes)


class _Errors:
    """Collects every problem so the operator sees the whole list at once."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.items.append("  {}: {}".format(where, message))

    def raise_if_any(self, path: Path) -> None:
        if self.items:
            raise ConfigError(
                "{} is not a usable content config. {} problem(s):\n{}".format(
                    path, len(self.items), "\n".join(self.items)
                )
            )


def _require(
    errs: _Errors, where: str, data: Any, key: str, kind: type, *, default: Any = None
) -> Any:
    """Pull one typed key. Records an error and returns None when unusable."""
    if not isinstance(data, dict) or key not in data or data[key] is None:
        if default is None:
            errs.add("{}.{}".format(where, key), "is required and missing")
            return None
        return default
    value = data[key]
    if kind is bool:
        if isinstance(value, bool):
            return value
    elif kind is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif kind is str:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                errs.add("{}.{}".format(where, key), "is empty")
                return None
            return value
    elif isinstance(value, kind):
        return value
    errs.add(
        "{}.{}".format(where, key),
        "must be {}, got {}".format(kind.__name__, type(value).__name__),
    )
    return None


def _parse_constraints(
    errs: _Errors, where: str, data: Any, *, defaults: Constraints | None
) -> Constraints | None:
    if data is None:
        if defaults is not None:
            return None  # a lane may omit its block and inherit the global one
        errs.add(where, "is required and missing")
        return None
    if not isinstance(data, dict):
        errs.add(where, "must be a mapping, got {}".format(type(data).__name__))
        return None

    fallback_len = defaults.max_length if defaults else PLATFORM_MAX_LENGTH
    max_length = _require(errs, where, data, "max_length", int, default=fallback_len)
    allow_emojis = _require(
        errs, where, data, "allow_emojis", bool,
        default=defaults.allow_emojis if defaults else False,
    )
    forbid_em_dash = _require(
        errs, where, data, "forbid_em_dash", bool,
        default=defaults.forbid_em_dash if defaults else True,
    )

    if isinstance(max_length, int):
        if max_length < 1:
            errs.add(where + ".max_length", "must be at least 1")
        if max_length > PLATFORM_MAX_LENGTH:
            errs.add(
                where + ".max_length",
                "is {}, above the platform limit of {}".format(
                    max_length, PLATFORM_MAX_LENGTH
                ),
            )

    rules_raw = data.get("rules") or []
    rules: list[str] = []
    if not isinstance(rules_raw, list):
        errs.add(where + ".rules", "must be a list, got {}".format(type(rules_raw).__name__))
    else:
        for i, rule in enumerate(rules_raw):
            if not isinstance(rule, str) or not rule.strip():
                errs.add("{}.rules[{}]".format(where, i), "must be a non-empty string")
            else:
                rules.append(rule.strip())

    if max_length is None or allow_emojis is None or forbid_em_dash is None:
        return None
    return Constraints(
        max_length=max_length,
        allow_emojis=allow_emojis,
        forbid_em_dash=forbid_em_dash,
        rules=tuple(rules),
    )


def _parse_lane(
    errs: _Errors,
    i: int,
    data: Any,
    globals_: Constraints | None,
    seen_lane_ids: set[str],
    seen_source_ids: set[str],
) -> LaneConfig | None:
    where = "lanes[{}]".format(i)
    if not isinstance(data, dict):
        errs.add(where, "must be a mapping, got {}".format(type(data).__name__))
        return None

    lane_id = _require(errs, where, data, "id", str)
    if lane_id:
        where = "lanes[{}] ({})".format(i, lane_id)
        if not ID_RE.match(lane_id):
            errs.add(where + ".id", "must be lowercase letters, digits and underscores")
        if lane_id in seen_lane_ids:
            errs.add(where + ".id", "is a duplicate of an earlier lane")
        seen_lane_ids.add(lane_id)

    audience = _require(errs, where, data, "audience", str)
    if audience and audience not in AUDIENCES:
        errs.add(
            where + ".audience",
            "is {!r}, must be one of {}".format(audience, sorted(AUDIENCES)),
        )

    post_type = _require(errs, where, data, "post_type", str)
    purpose = _require(errs, where, data, "purpose", str)
    example = _require(errs, where, data, "example", str)
    active = _require(errs, where, data, "active", bool, default=True)
    allow_images = _require(errs, where, data, "allow_images", bool, default=False)
    constraints = _parse_constraints(
        errs, where + ".constraints", data.get("constraints"), defaults=globals_
    )

    sources_raw = data.get("sources") or []
    sources: list[SourceConfig] = []
    if not isinstance(sources_raw, list):
        errs.add(where + ".sources", "must be a list, got {}".format(type(sources_raw).__name__))
    elif not sources_raw and active is not False:
        errs.add(where + ".sources", "an active lane needs at least one source")
    else:
        for j, src in enumerate(sources_raw):
            swhere = "{}.sources[{}]".format(where, j)
            if not isinstance(src, dict):
                errs.add(swhere, "must be a mapping, got {}".format(type(src).__name__))
                continue
            sid = _require(errs, swhere, src, "id", str)
            stype = _require(errs, swhere, src, "source_type", str)
            loc = _require(errs, swhere, src, "location", str)
            if sid:
                if not ID_RE.match(sid):
                    errs.add(swhere + ".id", "must be lowercase letters, digits and underscores")
                if sid in seen_source_ids:
                    errs.add(swhere + ".id", "is a duplicate of an earlier source")
                seen_source_ids.add(sid)
            if stype and stype not in SOURCE_TYPES:
                errs.add(
                    swhere + ".source_type",
                    "is {!r}, must be one of {}".format(stype, sorted(SOURCE_TYPES)),
                )
            if sid and stype and loc and lane_id:
                sources.append(SourceConfig(sid, lane_id, stype, loc))

    if None in (lane_id, audience, post_type, purpose, example) or active is None:
        return None
    return LaneConfig(
        id=lane_id,
        audience=audience,
        post_type=post_type,
        purpose=" ".join(purpose.split()),
        example=" ".join(example.split()),
        constraints=constraints,
        active=active,
        allow_images=bool(allow_images),
        sources=tuple(sources),
    )


def _parse_images(errs: _Errors, data: Any) -> ImageSettings:
    """The images block is optional. Absent means no images anywhere."""
    if data is None:
        return ImageSettings(enabled=False, model="gpt-image-1", size="1024x1024",
                             quality="medium")
    if not isinstance(data, dict):
        errs.add("images", "must be a mapping, got {}".format(type(data).__name__))
        return ImageSettings(False, "gpt-image-1", "1024x1024", "medium")

    enabled = _require(errs, "images", data, "enabled", bool, default=False)
    model = _require(errs, "images", data, "model", str, default="gpt-image-1")
    size = _require(errs, "images", data, "size", str, default="1024x1024")
    quality = _require(errs, "images", data, "quality", str, default="medium")
    alt_max = _require(errs, "images", data, "alt_text_max", int, default=ALT_TEXT_MAX)

    if size and size not in IMAGE_SIZES:
        errs.add("images.size", "is {!r}, must be one of {}".format(size, sorted(IMAGE_SIZES)))
    if quality and quality not in IMAGE_QUALITIES:
        errs.add(
            "images.quality",
            "is {!r}, must be one of {}".format(quality, sorted(IMAGE_QUALITIES)),
        )
    if isinstance(alt_max, int) and not (1 <= alt_max <= ALT_TEXT_MAX):
        errs.add("images.alt_text_max", "must be between 1 and {}".format(ALT_TEXT_MAX))

    return ImageSettings(
        enabled=bool(enabled),
        model=model or "gpt-image-1",
        size=size or "1024x1024",
        quality=quality or "medium",
        alt_text_max=alt_max if isinstance(alt_max, int) else ALT_TEXT_MAX,
    )


def _parse_slot(
    errs: _Errors, i: int, data: Any, lane_ids: set[str], seen: set[str]
) -> SlotConfig | None:
    where = "schedule[{}]".format(i)
    if not isinstance(data, dict):
        errs.add(where, "must be a mapping, got {}".format(type(data).__name__))
        return None

    slot_id = _require(errs, where, data, "id", str)
    if slot_id:
        where = "schedule[{}] ({})".format(i, slot_id)
        if slot_id in seen:
            errs.add(where + ".id", "is a duplicate of an earlier slot")
        seen.add(slot_id)

    lane_id = _require(errs, where, data, "lane_id", str)
    if lane_id and lane_id not in lane_ids:
        errs.add(where + ".lane_id", "{!r} does not match any lane".format(lane_id))

    day = _require(errs, where, data, "day_of_week", str)
    if day:
        day = day.lower()[:3]
        if day not in DAYS:
            errs.add(where + ".day_of_week", "must be one of {}".format(DAYS))
            day = None

    raw_time = data.get("time")
    time_s = None
    if isinstance(raw_time, str) and TIME_RE.match(raw_time.strip()):
        time_s = raw_time.strip()
    else:
        errs.add(
            where + ".time",
            "must be a quoted 24-hour HH:MM string, got {!r}".format(raw_time),
        )

    active = _require(errs, where, data, "active", bool, default=True)

    if None in (slot_id, lane_id, day, time_s) or active is None:
        return None
    return SlotConfig(slot_id, lane_id, day, time_s, active)


def load_config(path: str | Path) -> ContentConfig:
    """Read and validate the content YAML. Raises ConfigError listing every problem."""
    path = Path(path)
    if not path.exists():
        raise ConfigError("content config not found at {}".format(path.resolve()))

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError("{} is not valid YAML:\n  {}".format(path, exc)) from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            "{} must be a mapping at the top level, got {}".format(
                path, type(raw).__name__
            )
        )

    errs = _Errors()
    timezone = _require(errs, "root", raw, "timezone", str, default="UTC")
    pull_interval = _require(errs, "root", raw, "pull_interval_minutes", int, default=360)
    lead_hours = _require(errs, "root", raw, "generate_lead_hours", int, default=36)
    if isinstance(pull_interval, int) and pull_interval < 1:
        errs.add("root.pull_interval_minutes", "must be at least 1")
    if isinstance(lead_hours, int) and lead_hours < 1:
        errs.add("root.generate_lead_hours", "must be at least 1")

    globals_ = _parse_constraints(
        errs, "global_constraints", raw.get("global_constraints"), defaults=None
    )
    images = _parse_images(errs, raw.get("images"))

    lanes_raw = raw.get("lanes")
    lanes: list[LaneConfig] = []
    if not isinstance(lanes_raw, list) or not lanes_raw:
        errs.add("lanes", "must be a non-empty list")
    else:
        seen_lane_ids: set[str] = set()
        seen_source_ids: set[str] = set()
        for i, lane_raw in enumerate(lanes_raw):
            lane = _parse_lane(errs, i, lane_raw, globals_, seen_lane_ids, seen_source_ids)
            if lane:
                lanes.append(lane)

    schedule_raw = raw.get("schedule")
    schedule: list[SlotConfig] = []
    lane_ids = {lane.id for lane in lanes}
    if not isinstance(schedule_raw, list) or not schedule_raw:
        errs.add("schedule", "must be a non-empty list")
    else:
        seen_slot_ids: set[str] = set()
        for i, slot_raw in enumerate(schedule_raw):
            slot = _parse_slot(errs, i, slot_raw, lane_ids, seen_slot_ids)
            if slot:
                schedule.append(slot)

    if isinstance(timezone, str):
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(timezone)
        except Exception as exc:  # noqa: BLE001 - reported to the operator verbatim
            errs.add(
                "root.timezone",
                "{!r} is not a timezone this machine knows ({}). On Windows, "
                "non-UTC zones need the tzdata package installed.".format(timezone, exc),
            )

    # A lane asking for images when the system has them switched off is a
    # contradiction worth naming, not a silent no-op.
    if not images.enabled:
        for lane in lanes:
            if lane.allow_images:
                errs.add(
                    "lanes ({})".format(lane.id),
                    "sets allow_images but images.enabled is false, so it would "
                    "never get one. Turn images on, or drop allow_images.",
                )

    errs.raise_if_any(path)

    return ContentConfig(
        timezone=timezone,
        pull_interval_minutes=pull_interval,
        generate_lead_hours=lead_hours,
        global_constraints=globals_,
        lanes=tuple(lanes),
        schedule=tuple(schedule),
        images=images,
        path=path,
    )
