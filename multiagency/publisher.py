"""Publishing a single text post to X, and a mock that writes to a file.

OAuth 1.0a is signed by requests-oauthlib. The body is sent as JSON, so
oauthlib signs only the oauth_* parameters and leaves the body out of the
signature base string, which is what the v2 endpoint expects.

Credentials arrive as arguments, are never stored, and are never logged. The
error messages below deliberately quote the platform response but never the
Authorization header.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests
from oauthlib.oauth1 import SIGNATURE_HMAC_SHA1, SIGNATURE_TYPE_AUTH_HEADER
from requests_oauthlib import OAuth1

from .clock import iso, now_utc
from .errors import PublishError
from .settings import REPO_ROOT

log = logging.getLogger(__name__)

X_TWEETS_ENDPOINT = "https://api.x.com/2/tweets"
POST_URL_TEMPLATE = "https://x.com/i/web/status/{}"
REQUEST_TIMEOUT_SECONDS = 30


class Publisher(Protocol):
    def publish(self, text: str) -> str:
        """Return the platform post id. Raise PublishError on refusal."""


@dataclass
class MockPublisher:
    """Writes to a local file instead of the network. Used until the end.

    It is a stand-in for the platform, not for the approval gate: it is only
    ever reached by a post a human already approved.
    """

    path: Path = REPO_ROOT / "data" / "published_mock.log"
    counter: int = 0

    def publish(self, text: str) -> str:
        self.counter += 1
        post_id = "mock-{}-{}".format(int(time.time()), self.counter)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"id": post_id, "at": iso(now_utc()), "text": text}) + "\n"
            )
        log.info("mock publish %s (%d chars)", post_id, len(text))
        return post_id


class XPublisher:
    """X API v2, POST /2/tweets, OAuth 1.0a user context."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_token_secret: str,
        endpoint: str = X_TWEETS_ENDPOINT,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("X_API_KEY", api_key),
                ("X_API_SECRET", api_secret),
                ("X_ACCESS_TOKEN", access_token),
                ("X_ACCESS_TOKEN_SECRET", access_token_secret),
            )
            if not value
        ]
        if missing:
            raise PublishError(
                "PUBLISHER=x but these are not set in the environment: "
                + ", ".join(missing)
            )
        # oauthlib holds the secrets. They are not copied onto this object, so
        # they cannot leak through a repr or a traceback frame of ours.
        self.auth = OAuth1(
            client_key=api_key,
            client_secret=api_secret,
            resource_owner_key=access_token,
            resource_owner_secret=access_token_secret,
            signature_method=SIGNATURE_HMAC_SHA1,
            signature_type=SIGNATURE_TYPE_AUTH_HEADER,
        )
        self.endpoint = endpoint
        self.session = requests.Session()

    def publish(self, text: str) -> str:
        try:
            response = self.session.post(
                self.endpoint,
                json={"text": text},
                auth=self.auth,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise PublishError("could not reach X: {}".format(exc)) from exc

        if response.status_code >= 400:
            raise PublishError(
                "X returned HTTP {}: {}".format(response.status_code, response.text[:500])
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PublishError(
                "X returned a response that is not JSON: {}".format(response.text[:200])
            ) from exc

        post_id = (payload.get("data") or {}).get("id")
        if not post_id:
            raise PublishError(
                "X accepted the request but returned no post id: {}".format(payload)
            )
        return str(post_id)


def post_url(x_post_id: str | None) -> str | None:
    if not x_post_id:
        return None
    if x_post_id.startswith("mock-"):
        return None
    return POST_URL_TEMPLATE.format(x_post_id)


def get_publisher(settings) -> Publisher:
    """Pick the publisher from settings. Unknown values fail loudly."""
    choice = settings.publisher
    if choice == "mock":
        log.info("Publisher: mock")
        return MockPublisher()
    if choice == "x":
        log.info("Publisher: X API v2")
        return XPublisher(**settings.x_credentials)
    raise PublishError("PUBLISHER is {!r}, expected 'mock' or 'x'".format(choice))
