"""The approval queue: one page, three actions.

This is the only way a post reaches ``approved``, and ``approved`` is the only
state the publisher will read. There is no route here that publishes.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db, pipeline
from .clock import iso, now_utc
from .config import ContentConfig
from .settings import Settings
from .templates import render_queue

log = logging.getLogger(__name__)

PLATFORM_HARD_LIMIT = pipeline.PLATFORM_HARD_LIMIT


def _auth_dependency(settings: Settings):
    """Basic auth when a username and password are configured, otherwise none."""
    if not (settings.ui_username and settings.ui_password):
        def open_access() -> None:
            return None

        return open_access

    scheme = HTTPBasic()

    # Written as a default rather than Annotated[...]: this module uses
    # postponed annotations, and FastAPI resolves annotation strings against
    # module globals, where the closure variable `scheme` does not exist.
    def check(credentials: HTTPBasicCredentials = Depends(scheme)) -> None:
        user_ok = secrets.compare_digest(credentials.username, settings.ui_username)
        pass_ok = secrets.compare_digest(credentials.password, settings.ui_password)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="not authorised",
                headers={"WWW-Authenticate": "Basic"},
            )

    return check


def create_app(
    conn: sqlite3.Connection,
    cfg: ContentConfig,
    settings: Settings,
    *,
    lifespan=None,
) -> FastAPI:
    app = FastAPI(title="MultiAgency approval queue", lifespan=lifespan)
    guard = Depends(_auth_dependency(settings))

    app.state.conn = conn
    app.state.cfg = cfg
    app.state.settings = settings

    def _back(post_id: int | None = None, error: str = "") -> RedirectResponse:
        target = "/"
        if error:
            target += "?error=" + quote(error)
        if post_id:
            target += "#post-{}".format(post_id)
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)

    def _require_post(post_id: int) -> sqlite3.Row:
        row = db.get_post(conn, post_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no post {}".format(post_id))
        return row

    @app.get("/", response_class=HTMLResponse, dependencies=[guard])
    def queue(request: Request) -> HTMLResponse:
        with db.LOCK:
            pending = conn.execute(
                """
                SELECT p.*, m.raw_content AS material_content,
                       m.source_id AS material_source,
                       (p.scheduled_for IS NOT NULL AND p.scheduled_for <= ?) AS is_missed
                FROM posts p LEFT JOIN material m ON m.id = p.material_id
                WHERE p.status = 'pending'
                ORDER BY p.scheduled_for IS NULL, p.scheduled_for ASC, p.id ASC
                """,
                (iso(now_utc()),),
            ).fetchall()
            approved = db.posts_by_status(conn, ["approved"])
            posted = db.posts_by_status(conn, ["posted"], order="posted_at DESC")
            failed = db.posts_by_status(conn, ["failed"], order="scheduled_for DESC")
            material_counts = db.unused_material_counts(conn)
            events = db.recent_events(conn, 25)

        html = render_queue(
            cfg=cfg,
            pending=pending,
            approved=approved,
            posted=posted,
            failed=failed,
            material_counts=material_counts,
            events=events,
            publisher_name=settings.publisher,
            hermes_name=settings.hermes_mode,
        )
        error = request.query_params.get("error")
        if error:
            html = html.replace(
                "<main>",
                '<main><div class="banner bad">{}</div>'.format(
                    error.replace("<", "&lt;")
                ),
                1,
            )
        return HTMLResponse(html)

    @app.post("/posts/{post_id}/approve", dependencies=[guard])
    def approve(post_id: int, text: Annotated[str, Form()] = "") -> RedirectResponse:
        with db.LOCK:
            row = _require_post(post_id)
            if row["status"] not in ("pending", "approved"):
                return _back(post_id, "post {} is {}, so it cannot be approved".format(
                    post_id, row["status"]
                ))

            submitted = text.strip()
            if not submitted:
                return _back(post_id, "post {} has no text to approve".format(post_id))
            if len(submitted) > PLATFORM_HARD_LIMIT:
                # Save the work, refuse the approval, say why.
                pipeline.save_edit(conn, post_id, submitted)
                return _back(
                    post_id,
                    "post {} is {} characters, over the {} character platform limit. "
                    "The edit was saved and the post is still pending.".format(
                        post_id, len(submitted), PLATFORM_HARD_LIMIT
                    ),
                )

            edited = submitted if submitted != row["generated_text"].strip() else None
            pipeline.approve(conn, post_id, edited)
        log.info("post %s approved", post_id)
        return _back(post_id)

    @app.post("/posts/{post_id}/edit", dependencies=[guard])
    def edit(post_id: int, text: Annotated[str, Form()] = "") -> RedirectResponse:
        with db.LOCK:
            _require_post(post_id)
            if not text.strip():
                return _back(post_id, "post {} cannot be saved empty".format(post_id))
            pipeline.save_edit(conn, post_id, text)
        return _back(post_id)

    @app.post("/posts/{post_id}/reject", dependencies=[guard])
    def reject(post_id: int) -> RedirectResponse:
        with db.LOCK:
            _require_post(post_id)
            pipeline.reject(conn, post_id)
        log.info("post %s rejected", post_id)
        return _back()

    @app.post("/posts/{post_id}/unapprove", dependencies=[guard])
    def unapprove(post_id: int) -> RedirectResponse:
        """Pull a post back for another look. Posted rows are left alone."""
        with db.LOCK:
            row = _require_post(post_id)
            if row["status"] == "posted":
                return _back(post_id, "post {} is already published".format(post_id))
            db.set_status(conn, post_id, "pending", reviewed_at=None, failure_reason=None)
            db.log_event(conn, "returned_to_pending", "", post_id=post_id)
        return _back(post_id)

    @app.get("/healthz", dependencies=[guard])
    def healthz() -> dict[str, object]:
        with db.LOCK:
            counts = db.counts_by_status(conn)
        return {"ok": True, "posts": counts, "lanes": len(cfg.lanes)}

    return app
