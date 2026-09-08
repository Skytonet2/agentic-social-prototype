"""The single seam between this system and Hermes.

Everything upstream of here talks in HermesRequest and HermesResponse. Swapping
how Hermes is reached, or mocking it for tests, means changing this file only.
"""

from __future__ import annotations

import logging
from typing import Protocol

import requests

from ..errors import HermesError
from .contract import HermesRequest, HermesResponse

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60


class Hermes(Protocol):
    """The whole interface. One call, one post."""

    def generate(self, request: HermesRequest) -> HermesResponse:
        ...


class HttpHermes:
    """Hermes over HTTP.

    Assumes an endpoint that accepts the request payload as JSON and answers
    with the four contract fields. If the real Hermes speaks a different shape,
    or turns out to be a Python library rather than a service, ``call_raw`` is
    the one method to change and nothing else moves.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str = "",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not endpoint:
            raise HermesError(
                "HERMES_MODE=live but HERMES_ENDPOINT is not set. Set it in .env "
                "or run with HERMES_MODE=mock."
            )
        self.endpoint = endpoint
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if api_key:
            self.session.headers["Authorization"] = "Bearer {}".format(api_key)

    def generate(self, request: HermesRequest) -> HermesResponse:
        payload = self.call_raw(request)
        return HermesResponse.from_payload(payload, request)

    def call_raw(self, request: HermesRequest) -> object:
        """Send the request and return whatever came back, unchecked.

        Public because `main.py hermes-check` needs to show the operator the
        raw response when the shape does not match the contract.
        """
        try:
            response = self.session.post(
                self.endpoint, json=request.to_payload(), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise HermesError(
                "could not reach Hermes at {}: {}".format(self.endpoint, exc)
            ) from exc

        if response.status_code >= 400:
            raise HermesError(
                "Hermes returned HTTP {}: {}".format(
                    response.status_code, response.text[:500]
                )
            )

        try:
            return response.json()
        except ValueError as exc:
            raise HermesError(
                "Hermes returned something that is not JSON: {}".format(
                    response.text[:200]
                )
            ) from exc


def get_hermes(settings) -> Hermes:
    """Pick the adapter from settings. Unknown modes fail loudly."""
    mode = settings.hermes_mode
    if mode == "mock":
        from .mock import MockHermes

        log.info("Hermes adapter: mock")
        return MockHermes()
    if mode == "live":
        log.info("Hermes adapter: live at %s", settings.hermes_endpoint)
        return HttpHermes(settings.hermes_endpoint, settings.hermes_api_key)
    raise HermesError(
        "HERMES_MODE is {!r}, expected 'mock' or 'live'".format(mode)
    )
