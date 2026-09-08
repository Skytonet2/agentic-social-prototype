"""Rendering an image from a prompt Hermes wrote.

This module makes no decisions. It does not choose whether a post gets an
image, what the image should show, or what the alt text says. Hermes decides
all of that and hands over a prompt; this executes it, exactly as the publisher
executes an approved post. Keeping the judgment in one place is the reason the
rest of the system is debuggable.

Credentials are read from settings and used here only. They are never stored,
never logged, and never rendered in the UI.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

from .errors import ImageError
from .settings import REPO_ROOT

log = logging.getLogger(__name__)

OPENAI_IMAGES_ENDPOINT = "https://api.openai.com/v1/images/generations"
REQUEST_TIMEOUT_SECONDS = 120
IMAGE_DIR = REPO_ROOT / "data" / "images"

# X rejects images above 5MB on the simple upload path.
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class RenderedImage:
    """An image on disk, ready for a human to look at before it goes anywhere."""

    path: Path
    mime: str
    prompt: str

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size

    def read(self) -> bytes:
        return self.path.read_bytes()


class ImageRenderer(Protocol):
    def render(self, prompt: str, *, post_id: int) -> RenderedImage:
        """Return the rendered image. Raise ImageError if it could not be made."""


def _target_path(post_id: int, suffix: str = ".png") -> Path:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGE_DIR / "post-{}{}".format(post_id, suffix)


class MockImageRenderer:
    """Writes a small placeholder PNG. No network, deterministic per prompt.

    Real enough to exercise the whole path: a file on disk, a preview in the
    queue, bytes uploaded by the mock publisher.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.calls: list[str] = []

    def render(self, prompt: str, *, post_id: int) -> RenderedImage:
        self.calls.append(prompt)
        if self.fail_with is not None:
            raise self.fail_with

        # Colour derived from the prompt, so two different prompts are visibly
        # different when reviewing the queue.
        digest = hashlib.sha256(prompt.encode("utf-8")).digest()
        png = _solid_png(digest[0], digest[1], digest[2])

        path = _target_path(post_id)
        path.write_bytes(png)
        log.info("mock image rendered for post %s (%d bytes)", post_id, len(png))
        return RenderedImage(path=path, mime="image/png", prompt=prompt)


def _solid_png(r: int, g: int, b: int, size: int = 64) -> bytes:
    """A valid single-colour PNG, built by hand to avoid an image dependency."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + tag
            + payload
            + (binascii.crc32(tag + payload) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    header = size.to_bytes(4, "big") + size.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    row = b"\x00" + bytes([r, g, b]) * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


class OpenAIImageRenderer:
    """OpenAI image generation. Executes a prompt, nothing more."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-image-1",
        size: str = "1024x1024",
        quality: str = "medium",
        endpoint: str = OPENAI_IMAGES_ENDPOINT,
    ) -> None:
        if not api_key:
            raise ImageError(
                "IMAGE_RENDERER=openai but OPENAI_API_KEY is not set. Set it in "
                ".env, or run with IMAGE_RENDERER=mock."
            )
        self.model = model
        self.size = size
        self.quality = quality
        self.endpoint = endpoint
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer {}".format(api_key)})

    def render(self, prompt: str, *, post_id: int) -> RenderedImage:
        body = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "quality": self.quality,
            "n": 1,
        }
        try:
            response = self.session.post(
                self.endpoint, json=body, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise ImageError("could not reach the image model: {}".format(exc)) from exc

        if response.status_code >= 400:
            raise ImageError(
                "image model returned HTTP {}: {}".format(
                    response.status_code, response.text[:500]
                )
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ImageError("image model returned a response that is not JSON") from exc

        data = payload.get("data") or []
        if not data:
            raise ImageError("image model returned no image: {}".format(json.dumps(payload)[:300]))

        encoded = data[0].get("b64_json")
        if not encoded:
            raise ImageError(
                "image model returned no b64_json. Only inline image data is "
                "accepted, so that nothing depends on a URL that expires."
            )

        try:
            raw = base64.b64decode(encoded)
        except (binascii.Error, ValueError) as exc:
            raise ImageError("image model returned data that is not valid base64") from exc

        if len(raw) > MAX_IMAGE_BYTES:
            raise ImageError(
                "the image is {} bytes, over the {} byte upload limit".format(
                    len(raw), MAX_IMAGE_BYTES
                )
            )

        path = _target_path(post_id)
        path.write_bytes(raw)
        log.info("image rendered for post %s (%d bytes)", post_id, len(raw))
        return RenderedImage(path=path, mime="image/png", prompt=prompt)


def get_renderer(settings, cfg) -> ImageRenderer:
    """Pick the renderer from settings. Unknown values fail loudly."""
    choice = settings.image_renderer
    if choice == "mock":
        log.info("Image renderer: mock")
        return MockImageRenderer()
    if choice == "openai":
        log.info("Image renderer: OpenAI %s", cfg.images.model)
        return OpenAIImageRenderer(
            settings.openai_api_key,
            model=cfg.images.model,
            size=cfg.images.size,
            quality=cfg.images.quality,
        )
    raise ImageError(
        "IMAGE_RENDERER is {!r}, expected 'mock' or 'openai'".format(choice)
    )
