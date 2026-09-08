"""Runtime settings from the environment, seeded from .env.

Credentials are read here and nowhere else. They are never written to the
database, never logged, and never rendered in the UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> None:
    """Minimal .env reader. Real environment variables win unless override."""
    path = Path(path) if path else REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    config_path: Path
    db_path: Path
    hermes_mode: str
    hermes_endpoint: str
    hermes_api_key: str
    publisher: str
    image_renderer: str
    openai_api_key: str
    ui_username: str
    ui_password: str

    @property
    def x_credentials(self) -> dict[str, str]:
        return {
            "api_key": os.environ.get("X_API_KEY", ""),
            "api_secret": os.environ.get("X_API_SECRET", ""),
            "access_token": os.environ.get("X_ACCESS_TOKEN", ""),
            "access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
        }


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        config_path=_resolve(os.environ.get("CONFIG_PATH", "config/content.yaml")),
        db_path=_resolve(os.environ.get("DB_PATH", "data/social.db")),
        hermes_mode=os.environ.get("HERMES_MODE", "mock").strip().lower(),
        hermes_endpoint=os.environ.get("HERMES_ENDPOINT", "").strip(),
        hermes_api_key=os.environ.get("HERMES_API_KEY", "").strip(),
        publisher=os.environ.get("PUBLISHER", "mock").strip().lower(),
        image_renderer=os.environ.get("IMAGE_RENDERER", "mock").strip().lower(),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        ui_username=os.environ.get("UI_USERNAME", "").strip(),
        ui_password=os.environ.get("UI_PASSWORD", ""),
    )
