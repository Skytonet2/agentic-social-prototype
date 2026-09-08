"""SQLite state. One file, no migrations framework, no ORM.

The schema follows the agreed data model. Three columns are additions and are
called out here so they are not mistaken for drift:

* ``posts.flagged`` / ``posts.flag_reason`` carry step 3 of the flow. A post
  that still violates a hard constraint after one regeneration is queued as
  ``pending`` and flagged, never dropped, so the reviewer sees why.
* ``posts.slot_id`` ties a post to the schedule row that asked for it, which is
  what stops the generator filling the same slot twice.

The ``events`` table is an append-only operational log. Skipped slots and
publish failures land there so a week can be audited after the fact.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .clock import iso, now_utc
from .config import ContentConfig

# One connection is shared by the web threadpool and the scheduler thread.
# Every caller takes this lock around a unit of work, which is enough for a
# single-process prototype and keeps the concurrency story easy to read.
LOCK = threading.RLock()

STATUSES = ("pending", "approved", "rejected", "posted", "failed")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lanes (
    id          TEXT PRIMARY KEY,
    audience    TEXT NOT NULL,
    post_type   TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    example     TEXT NOT NULL,
    constraints TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sources (
    id             TEXT PRIMARY KEY,
    lane_id        TEXT NOT NULL REFERENCES lanes(id),
    source_type    TEXT NOT NULL,
    location       TEXT NOT NULL,
    last_pulled_at TEXT
);

CREATE TABLE IF NOT EXISTS material (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(id),
    raw_content TEXT NOT NULL,
    pulled_at   TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS posts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    lane_id        TEXT NOT NULL REFERENCES lanes(id),
    material_id    INTEGER REFERENCES material(id),
    slot_id        TEXT,
    generated_text TEXT NOT NULL,
    reasoning      TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL CHECK (
                       status IN ('pending','approved','rejected','posted','failed')),
    edited_text    TEXT,
    created_at     TEXT NOT NULL,
    reviewed_at    TEXT,
    scheduled_for  TEXT,
    posted_at      TEXT,
    x_post_id      TEXT,
    failure_reason TEXT,
    flagged        INTEGER NOT NULL DEFAULT 0,
    flag_reason    TEXT
);

CREATE TABLE IF NOT EXISTS schedule (
    id          TEXT PRIMARY KEY,
    lane_id     TEXT NOT NULL REFERENCES lanes(id),
    day_of_week TEXT NOT NULL,
    time        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    lane_id TEXT,
    post_id INTEGER,
    detail  TEXT
);

CREATE INDEX IF NOT EXISTS idx_material_unused ON material(source_id, used, id);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, scheduled_for);

-- One live post per slot occurrence. A rejected post frees the slot so the
-- next generation run can try again with different material.
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_slot_occurrence
    ON posts(slot_id, scheduled_for)
    WHERE slot_id IS NOT NULL AND status != 'rejected';
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log_event(
    conn: sqlite3.Connection,
    kind: str,
    detail: str = "",
    *,
    lane_id: str | None = None,
    post_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, lane_id, post_id, detail) VALUES (?,?,?,?,?)",
        (iso(now_utc()), kind, lane_id, post_id, detail),
    )


def sync_config(conn: sqlite3.Connection, cfg: ContentConfig) -> None:
    """Mirror the YAML into the tables.

    The YAML is the source of truth. Rows that disappear from it are marked
    inactive rather than deleted, because posts and material still point at them.
    """
    with _tx(conn):
        for lane in cfg.lanes:
            conn.execute(
                """
                INSERT INTO lanes (id, audience, post_type, purpose, example,
                                   constraints, active)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    audience=excluded.audience, post_type=excluded.post_type,
                    purpose=excluded.purpose, example=excluded.example,
                    constraints=excluded.constraints, active=excluded.active
                """,
                (
                    lane.id,
                    lane.audience,
                    lane.post_type,
                    lane.purpose,
                    lane.example,
                    json.dumps(asdict(cfg.constraints_for(lane.id))),
                    int(lane.active),
                ),
            )
        for src in cfg.sources:
            conn.execute(
                """
                INSERT INTO sources (id, lane_id, source_type, location)
                VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    lane_id=excluded.lane_id, source_type=excluded.source_type,
                    location=excluded.location
                """,
                (src.id, src.lane_id, src.source_type, src.location),
            )
        for slot in cfg.schedule:
            conn.execute(
                """
                INSERT INTO schedule (id, lane_id, day_of_week, time, active)
                VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    lane_id=excluded.lane_id, day_of_week=excluded.day_of_week,
                    time=excluded.time, active=excluded.active
                """,
                (slot.id, slot.lane_id, slot.day_of_week, slot.time, int(slot.active)),
            )

        _deactivate_missing(conn, "lanes", [lane.id for lane in cfg.lanes])
        _deactivate_missing(conn, "schedule", [s.id for s in cfg.schedule])


def _deactivate_missing(conn: sqlite3.Connection, table: str, keep: list[str]) -> None:
    placeholders = ",".join("?" * len(keep)) or "''"
    conn.execute(
        "UPDATE {} SET active = 0 WHERE id NOT IN ({})".format(table, placeholders),
        keep,
    )


class _tx:
    """Explicit transaction. isolation_level=None means we drive BEGIN ourselves."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


transaction = _tx


# --- material -------------------------------------------------------------


def insert_material(
    conn: sqlite3.Connection, source_id: str, raw_content: str, fingerprint: str
) -> int | None:
    """Insert one item. Returns None when the fingerprint is already known."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO material (source_id, raw_content, pulled_at, used, fingerprint)
        VALUES (?,?,?,0,?)
        """,
        (source_id, raw_content, iso(now_utc()), fingerprint),
    )
    return cur.lastrowid if cur.rowcount else None


def mark_source_pulled(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        "UPDATE sources SET last_pulled_at = ? WHERE id = ?", (iso(now_utc()), source_id)
    )


def next_unused_material(conn: sqlite3.Connection, lane_id: str) -> sqlite3.Row | None:
    """Oldest unused item belonging to any source of this lane."""
    return conn.execute(
        """
        SELECT m.* FROM material m
        JOIN sources s ON s.id = m.source_id
        WHERE s.lane_id = ? AND m.used = 0
        ORDER BY m.pulled_at ASC, m.id ASC
        LIMIT 1
        """,
        (lane_id,),
    ).fetchone()


def mark_material_used(conn: sqlite3.Connection, material_id: int) -> None:
    conn.execute("UPDATE material SET used = 1 WHERE id = ?", (material_id,))


def unused_material_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.lane_id AS lane_id,
               SUM(CASE WHEN m.used = 0 THEN 1 ELSE 0 END) AS unused,
               COUNT(m.id) AS total
        FROM sources s LEFT JOIN material m ON m.source_id = s.id
        GROUP BY s.lane_id ORDER BY s.lane_id
        """
    ).fetchall()


# --- posts ----------------------------------------------------------------


def insert_post(
    conn: sqlite3.Connection,
    *,
    lane_id: str,
    material_id: int | None,
    slot_id: str | None,
    generated_text: str,
    reasoning: str,
    scheduled_for: datetime | None,
    flagged: bool = False,
    flag_reason: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO posts (lane_id, material_id, slot_id, generated_text, reasoning,
                           status, created_at, scheduled_for, flagged, flag_reason)
        VALUES (?,?,?,?,?,'pending',?,?,?,?)
        """,
        (
            lane_id,
            material_id,
            slot_id,
            generated_text,
            reasoning,
            iso(now_utc()),
            iso(scheduled_for),
            int(flagged),
            flag_reason,
        ),
    )
    return int(cur.lastrowid)


def get_post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT p.*, m.raw_content AS material_content, m.source_id AS material_source
        FROM posts p LEFT JOIN material m ON m.id = p.material_id
        WHERE p.id = ?
        """,
        (post_id,),
    ).fetchone()


def posts_by_status(
    conn: sqlite3.Connection, statuses: Iterable[str], order: str = "scheduled_for ASC"
) -> list[sqlite3.Row]:
    statuses = list(statuses)
    placeholders = ",".join("?" * len(statuses))
    return conn.execute(
        """
        SELECT p.*, m.raw_content AS material_content, m.source_id AS material_source
        FROM posts p LEFT JOIN material m ON m.id = p.material_id
        WHERE p.status IN ({})
        ORDER BY {}
        """.format(placeholders, order),
        statuses,
    ).fetchall()


def slot_is_filled(
    conn: sqlite3.Connection, slot_id: str, scheduled_for: datetime
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM posts
        WHERE slot_id = ? AND scheduled_for = ? AND status != 'rejected' LIMIT 1
        """,
        (slot_id, iso(scheduled_for)),
    ).fetchone()
    return row is not None


def recent_published(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """The last posts that actually went out, newest first.

    This is what stops Hermes repeating itself by day four, so it reads the
    published text, meaning the edited version when a human changed it.
    """
    return conn.execute(
        """
        SELECT id, lane_id, posted_at,
               COALESCE(NULLIF(edited_text, ''), generated_text) AS text
        FROM posts
        WHERE status = 'posted'
        ORDER BY posted_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def final_text(row: sqlite3.Row | dict[str, Any]) -> str:
    """What publishes: the human edit when there is one, otherwise the draft."""
    edited = row["edited_text"]
    return edited if edited else row["generated_text"]


def set_status(
    conn: sqlite3.Connection,
    post_id: int,
    status: str,
    **fields: Any,
) -> None:
    if status not in STATUSES:
        raise ValueError("unknown status {!r}".format(status))
    columns = ["status = ?"]
    values: list[Any] = [status]
    for key, value in fields.items():
        columns.append("{} = ?".format(key))
        values.append(value)
    values.append(post_id)
    conn.execute(
        "UPDATE posts SET {} WHERE id = ?".format(", ".join(columns)), values
    )


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM posts GROUP BY status")
    out = {status: 0 for status in STATUSES}
    for row in rows:
        out[row["status"]] = row["n"]
    return out


def recent_events(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
