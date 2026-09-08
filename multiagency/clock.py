"""Time helpers.

Everything is stored in UTC as an ISO 8601 string so string ordering matches
chronological ordering. The configured timezone is presentation only: it is
what the schedule times in the YAML mean, and what the review UI displays.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("refusing to guess the timezone of a naive datetime")
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    """Serialise for storage. Always UTC, always the same shape."""
    if dt is None:
        return None
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def next_occurrence(
    weekday: int, hour: int, minute: int, tz_name: str, after: datetime
) -> datetime:
    """The first time this weekly slot falls strictly after ``after``, in UTC.

    The slot is defined in the configured timezone, so a system running in UTC
    still posts at 10:00 local for whoever set the schedule.
    """
    tz = zone(tz_name)
    local = to_utc(after).astimezone(tz)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - candidate.weekday()) % 7
    candidate = candidate + timedelta(days=days_ahead)
    if candidate <= local:
        candidate = candidate + timedelta(days=7)
    return to_utc(candidate)


def occurrences_within(
    weekday: int, hour: int, minute: int, tz_name: str, after: datetime, horizon_hours: int
) -> list[datetime]:
    """Every occurrence of a weekly slot inside the lead window."""
    limit = to_utc(after) + timedelta(hours=horizon_hours)
    out: list[datetime] = []
    cursor = after
    while True:
        nxt = next_occurrence(weekday, hour, minute, tz_name, cursor)
        if nxt > limit:
            return out
        out.append(nxt)
        cursor = nxt


def local_str(dt: datetime | None, tz_name: str) -> str:
    if dt is None:
        return ""
    return to_utc(dt).astimezone(zone(tz_name)).strftime("%a %d %b %H:%M")
