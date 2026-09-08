"""Pulling material into the material table.

Sources are dumb readers. They do not summarise, rank or filter on meaning,
because deciding what is worth posting is Hermes's job. They only produce
candidate items and refuse to store the same item twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import requests

from .config import SourceConfig
from .db import insert_material, log_event, mark_source_pulled
from .settings import REPO_ROOT

log = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 30
MIN_ITEM_CHARS = 20
USER_AGENT = "multiagency-social/0.1"


@dataclass
class PullResult:
    source_id: str
    new: int = 0
    duplicates: int = 0
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return "{}: failed ({})".format(self.source_id, self.error)
        return "{}: {} new, {} already known".format(
            self.source_id, self.new, self.duplicates
        )


def fingerprint(content: str) -> str:
    """Content identity for dedupe. Whitespace and case are not identity."""
    normalised = " ".join(content.split()).casefold()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _resolve(location: str) -> Path:
    path = Path(location)
    return path if path.is_absolute() else REPO_ROOT / path


def read_items(source: SourceConfig) -> list[str]:
    """Return candidate items from one source. Raises on an unreadable source."""
    reader = _READERS.get(source.source_type)
    if reader is None:
        raise ValueError("no reader for source_type {!r}".format(source.source_type))
    items = reader(source)
    return [i for i in (" ".join(x.split()) for x in items) if len(i) >= MIN_ITEM_CHARS]


def _read_file_lines(source: SourceConfig) -> list[str]:
    """One item per non-empty line. Lines starting with # are comments."""
    path = _resolve(source.location)
    if not path.exists():
        raise FileNotFoundError("source file not found: {}".format(path))
    return [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _read_directory(source: SourceConfig) -> list[str]:
    """One item per file in the directory, whole file as the item."""
    path = _resolve(source.location)
    if not path.is_dir():
        raise NotADirectoryError("source directory not found: {}".format(path))
    return [
        f.read_text(encoding="utf-8")
        for f in sorted(path.iterdir())
        if f.is_file() and f.suffix in {".txt", ".md"}
    ]


def _read_jsonl(source: SourceConfig) -> list[str]:
    """One item per line of JSON. Uses the text/content/body/title field."""
    path = _resolve(source.location)
    if not path.exists():
        raise FileNotFoundError("source file not found: {}".format(path))
    items: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("{} line {} is not valid JSON: {}".format(path, lineno, exc)) from exc
        if isinstance(obj, str):
            items.append(obj)
            continue
        if not isinstance(obj, dict):
            raise ValueError("{} line {} is not an object or a string".format(path, lineno))
        for key in ("text", "content", "body", "title"):
            if isinstance(obj.get(key), str) and obj[key].strip():
                items.append(obj[key])
                break
    return items


def _read_rss(source: SourceConfig) -> list[str]:
    """Title and summary per entry, for RSS or Atom."""
    try:
        response = requests.get(
            source.location,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ConnectionError("could not fetch {}: {}".format(source.location, exc)) from exc

    root = ElementTree.fromstring(response.content)
    items: list[str] = []
    entries = root.iter("item")
    for entry in entries:
        items.append(_entry_text(entry, "title", "description"))
    if not items:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            items.append(
                _entry_text(
                    entry,
                    "{http://www.w3.org/2005/Atom}title",
                    "{http://www.w3.org/2005/Atom}summary",
                )
            )
    return [i for i in items if i]


def _entry_text(entry, title_tag: str, body_tag: str) -> str:
    parts = []
    for tag in (title_tag, body_tag):
        node = entry.find(tag)
        if node is not None and node.text:
            parts.append(node.text.strip())
    return ". ".join(parts)


_READERS = {
    "file_lines": _read_file_lines,
    "directory": _read_directory,
    "jsonl": _read_jsonl,
    "rss": _read_rss,
}


def pull_source(conn: sqlite3.Connection, source: SourceConfig) -> PullResult:
    """Pull one source. A broken source is recorded, not raised.

    One unreachable feed must not stop the other sources from filling the
    queue, so the failure lands in the events log and the run continues.
    """
    result = PullResult(source_id=source.id)
    try:
        items = read_items(source)
    except Exception as exc:  # noqa: BLE001 - recorded and surfaced in the UI
        result.error = "{}: {}".format(type(exc).__name__, exc)
        log.warning("source %s failed: %s", source.id, result.error)
        log_event(conn, "source_failed", result.error, lane_id=source.lane_id)
        return result

    for item in items:
        if insert_material(conn, source.id, item, fingerprint(item)) is None:
            result.duplicates += 1
        else:
            result.new += 1

    mark_source_pulled(conn, source.id)
    log.info("%s", result)
    if result.new:
        log_event(
            conn,
            "material_pulled",
            "{} new item(s) from {}".format(result.new, source.id),
            lane_id=source.lane_id,
        )
    return result


def pull_all(conn: sqlite3.Connection, cfg) -> list[PullResult]:
    """Pull every source belonging to an active lane."""
    results = []
    for lane in cfg.lanes:
        if not lane.active:
            continue
        for source in lane.sources:
            results.append(pull_source(conn, source))
    return results
