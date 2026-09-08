"""Entry point. One process: scheduler plus the approval page.

    python -m multiagency.main serve       run the queue and the scheduler
    python -m multiagency.main pull        pull material once, then exit
    python -m multiagency.main generate    fill upcoming slots once, then exit
    python -m multiagency.main publish     publish anything approved and due
    python -m multiagency.main status      what is in the queue
    python -m multiagency.main check       validate the config and exit

Connecting to a real Hermes:

    python -m multiagency.main hermes-payload   print the request, send nothing
    python -m multiagency.main hermes-check     one live round trip, write nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import asynccontextmanager

from . import db, pipeline
from .config import load_config
from .errors import ConfigError, HermesError, ImageError, PublishError
from .hermes import get_hermes
from .images import get_renderer
from .publisher import get_publisher
from .settings import Settings, load_settings

log = logging.getLogger("multiagency")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def bootstrap(settings: Settings):
    """Load the config, open the database, mirror one into the other."""
    cfg = load_config(settings.config_path)
    conn = db.connect(settings.db_path)
    db.sync_config(conn, cfg)
    log.info(
        "config ok: %d lane(s), %d source(s), %d slot(s), timezone %s",
        len(cfg.lanes),
        len(cfg.sources),
        len(cfg.active_slots),
        cfg.timezone,
    )
    return cfg, conn


def cmd_check(settings: Settings) -> int:
    cfg = load_config(settings.config_path)
    print("config is valid: {}".format(settings.config_path))
    for lane in cfg.lanes:
        constraints = cfg.constraints_for(lane.id)
        print(
            "  lane {:<14} {:<9} {:<14} max {} chars, emojis {}, {} rule(s), {} source(s)".format(
                lane.id,
                lane.audience,
                lane.post_type,
                constraints.max_length,
                "allowed" if constraints.allow_emojis else "not allowed",
                len(constraints.rules),
                len(lane.sources),
            )
        )
    for slot in cfg.schedule:
        print(
            "  slot {:<18} {} {} {}".format(
                slot.id,
                slot.lane_id,
                slot.day_of_week,
                slot.time + ("" if slot.active else "  (inactive)"),
            )
        )
    return 0


def cmd_pull(settings: Settings) -> int:
    cfg, conn = bootstrap(settings)
    for result in pipeline.run_pull(conn, cfg):
        print(" ", result)
    return 0


def cmd_generate(settings: Settings) -> int:
    cfg, conn = bootstrap(settings)
    hermes = get_hermes(settings)
    renderer = get_renderer(settings, cfg) if cfg.images.enabled else None
    for outcome in pipeline.run_generation(conn, cfg, hermes, renderer=renderer):
        print(" ", outcome)
    return 0


def cmd_publish(settings: Settings) -> int:
    cfg, conn = bootstrap(settings)
    publisher = get_publisher(settings)
    pipeline.sweep_missed_slots(conn, cfg)
    outcomes = pipeline.publish_due(conn, cfg, publisher)
    if not outcomes:
        print("  nothing approved and due")
    for outcome in outcomes:
        print(" ", outcome)
    return 0


def cmd_status(settings: Settings) -> int:
    cfg, conn = bootstrap(settings)
    counts = db.counts_by_status(conn)
    print("posts: " + ", ".join("{} {}".format(v, k) for k, v in counts.items()))
    print("material:")
    for row in db.unused_material_counts(conn):
        print(
            "  {:<14} {} unused of {} pulled".format(
                row["lane_id"], row["unused"] or 0, row["total"] or 0
            )
        )
    flagged = conn.execute(
        "SELECT COUNT(*) AS n FROM posts WHERE flagged = 1 AND status = 'pending'"
    ).fetchone()["n"]
    if flagged:
        print("{} pending post(s) flagged for a constraint violation".format(flagged))
    return 0


def cmd_hermes_check(
    settings: Settings, lane_id: str | None, endpoint: str | None, show_payload: bool
) -> int:
    """One live round trip against Hermes, writing nothing.

    This is what to run first when pointing the system at a real endpoint.
    """
    from .hermes.adapter import HttpHermes
    from .probe import render, run_probe

    cfg, conn = bootstrap(settings)

    lane_id = lane_id or next((lane.id for lane in cfg.lanes if lane.active), None)
    if lane_id is None:
        print("no active lane to test with", file=sys.stderr)
        return 2
    try:
        cfg.lane(lane_id)
    except KeyError:
        print(
            "no lane {!r}. Lanes are: {}".format(
                lane_id, ", ".join(lane.id for lane in cfg.lanes)
            ),
            file=sys.stderr,
        )
        return 2

    target = endpoint or settings.hermes_endpoint
    if not target:
        print(
            "\nNo Hermes endpoint to call.\n\n"
            "  Set HERMES_ENDPOINT in .env, or pass --endpoint https://...\n"
            "  To see the request this system would send without sending it:\n"
            "    python -m multiagency.main hermes-payload\n",
            file=sys.stderr,
        )
        return 2

    hermes = HttpHermes(target, settings.hermes_api_key)
    print("Calling Hermes at {}\n".format(target))
    result = run_probe(conn, cfg, hermes, lane_id)
    print(render(result, show_payload=show_payload))
    return 0 if result.ok else 1


def cmd_hermes_payload(settings: Settings, lane_id: str | None) -> int:
    """Print the request this system would send, without sending it.

    Hand this to whoever owns Hermes to confirm the shape before connecting.
    """
    from .probe import build_probe_request

    cfg, conn = bootstrap(settings)
    lane_id = lane_id or next(lane.id for lane in cfg.lanes if lane.active)
    request = build_probe_request(conn, cfg, lane_id)
    print(json.dumps(request.to_payload(), indent=2))
    return 0


def cmd_serve(settings: Settings, host: str, port: int) -> int:
    import uvicorn

    from .scheduler import build_scheduler
    from .web import create_app

    cfg, conn = bootstrap(settings)
    hermes = get_hermes(settings)
    publisher = get_publisher(settings)
    renderer = get_renderer(settings, cfg) if cfg.images.enabled else None
    scheduler = build_scheduler(conn, cfg, hermes, publisher, renderer)

    @asynccontextmanager
    async def lifespan(app):
        # One pull and one generation pass at startup so the queue is not empty
        # while waiting for the first interval to come round.
        with db.LOCK:
            pipeline.run_pull(conn, cfg)
            pipeline.run_generation(conn, cfg, hermes, renderer=renderer)
            pipeline.sweep_missed_slots(conn, cfg)
        scheduler.start()
        log.info("approval queue on http://%s:%d", host, port)
        if not (settings.ui_username and settings.ui_password):
            log.warning(
                "the approval queue has no password. Keep it on localhost or put "
                "basic auth in front of it by setting UI_USERNAME and UI_PASSWORD."
            )
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            conn.close()

    app = create_app(conn, cfg, settings, lifespan=lifespan)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="multiagency", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate the content config and exit")
    sub.add_parser("pull", help="pull material from every source once")
    sub.add_parser("generate", help="fill upcoming slots with pending posts")
    sub.add_parser("publish", help="publish approved posts whose slot has arrived")
    sub.add_parser("status", help="show queue and material counts")
    serve = sub.add_parser("serve", help="run the scheduler and the approval queue")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    payload = sub.add_parser(
        "hermes-payload",
        help="print the request this system would send to Hermes, send nothing",
    )
    payload.add_argument("--lane", default=None)

    probe = sub.add_parser(
        "hermes-check",
        help="send one real request to Hermes and report what came back",
    )
    probe.add_argument("--lane", default=None, help="which lane to test with")
    probe.add_argument(
        "--endpoint", default=None, help="override HERMES_ENDPOINT for this call"
    )
    probe.add_argument(
        "--show-payload", action="store_true", help="also print the request sent"
    )

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if args.command == "hermes-payload" and not args.verbose:
        # The output is meant to be piped or pasted, so keep it pure JSON.
        logging.getLogger().setLevel(logging.WARNING)
    settings = load_settings()

    try:
        if args.command == "check":
            return cmd_check(settings)
        if args.command == "pull":
            return cmd_pull(settings)
        if args.command == "generate":
            return cmd_generate(settings)
        if args.command == "publish":
            return cmd_publish(settings)
        if args.command == "status":
            return cmd_status(settings)
        if args.command == "serve":
            return cmd_serve(settings, args.host, args.port)
        if args.command == "hermes-payload":
            return cmd_hermes_payload(settings, args.lane)
        if args.command == "hermes-check":
            return cmd_hermes_check(
                settings, args.lane, args.endpoint, args.show_payload
            )
    except ConfigError as exc:
        print("\nThe content config is not usable, so nothing started.\n", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2
    except (HermesError, PublishError, ImageError) as exc:
        print("\n{}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 3

    parser.error("unknown command {!r}".format(args.command))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
