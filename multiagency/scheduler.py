"""Timing. Four jobs, one process.

The publish job only ever looks at approved posts. Nothing here can move a post
into ``approved``; that is the reviewer's action in the web page.
"""

from __future__ import annotations

import logging
import sqlite3

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import db, pipeline
from .clock import zone
from .config import ContentConfig
from .hermes import Hermes
from .images import ImageRenderer
from .publisher import Publisher

log = logging.getLogger(__name__)

PUBLISH_CHECK_SECONDS = 60
GENERATE_CHECK_MINUTES = 30
SWEEP_CHECK_MINUTES = 5


def _guarded(name: str, fn):
    """Run a job under the shared lock and never let it kill the scheduler."""

    def job() -> None:
        try:
            with db.LOCK:
                fn()
        except Exception:  # noqa: BLE001 - a dead job is worse than a logged one
            log.exception("scheduled job %s failed", name)

    job.__name__ = name
    return job


def build_scheduler(
    conn: sqlite3.Connection,
    cfg: ContentConfig,
    hermes: Hermes,
    publisher: Publisher,
    renderer: ImageRenderer | None = None,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=zone(cfg.timezone))

    scheduler.add_job(
        _guarded("pull", lambda: pipeline.run_pull(conn, cfg)),
        IntervalTrigger(minutes=cfg.pull_interval_minutes),
        id="pull",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guarded(
            "generate",
            lambda: pipeline.run_generation(conn, cfg, hermes, renderer=renderer),
        ),
        IntervalTrigger(minutes=GENERATE_CHECK_MINUTES),
        id="generate",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guarded("publish", lambda: pipeline.publish_due(conn, cfg, publisher)),
        IntervalTrigger(seconds=PUBLISH_CHECK_SECONDS),
        id="publish",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guarded("sweep_missed_slots", lambda: pipeline.sweep_missed_slots(conn, cfg)),
        IntervalTrigger(minutes=SWEEP_CHECK_MINUTES),
        id="sweep",
        max_instances=1,
        coalesce=True,
    )

    log.info(
        "scheduler configured: pull every %d min, generate every %d min, "
        "publish check every %d s, missed-slot sweep every %d min, timezone %s",
        cfg.pull_interval_minutes,
        GENERATE_CHECK_MINUTES,
        PUBLISH_CHECK_SECONDS,
        SWEEP_CHECK_MINUTES,
        cfg.timezone,
    )
    return scheduler
