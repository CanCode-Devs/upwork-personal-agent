from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DASHBOARD_PASSWORD = "change-me"
DEFAULT_SESSION_SECRET = "replace-with-a-long-random-string"
SESSION_SECRET_FILENAME = "session_secret"
STUDIO_NAME = "CanCode Devs"
STUDIO_URL = "https://cancodedevs.com"
STUDIO_SLOGAN = "Ideas compile. Products ship."


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dashboard_username: str = "admin"
    dashboard_password: str = DEFAULT_DASHBOARD_PASSWORD
    session_secret: str = DEFAULT_SESSION_SECRET

    openai_api_key: str = ""
    openai_base_url: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_draft_model: str = "gpt-4o"

    app_name: str = "Upwork Personal Agent"
    app_tagline: str = "Adaptive agent"
    seed_demo_portfolio: bool = False
    scoring_path: Path = Field(default=Path("./profiles/scoring.yaml"))

    search_queries: str = "python fastapi"
    min_score: int = 70
    min_client_score: int = 50
    poll_interval_seconds: int = 900
    search_gap_seconds: int = 60
    find_jobs_min_interval_seconds: int = 5
    poll_cooldown_seconds: int = 300
    approval_ttl_hours: int = 24
    autonomy_mode: str = "manual"
    auto_submit_threshold: int = 85
    min_hourly: int | None = None
    min_fixed: int | None = None
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    database_url: str = "sqlite:///./data/app.db"
    data_dir: Path = Field(default=Path("./data"))
    profile_path: Path = Field(default=Path("./profiles/default.yaml"))

    upwork_mcp_url: str = "https://mcp.upwork.com/mcp"
    oauth_redirect_port: int = 8765
    oauth_redirect_host: str = "127.0.0.1"

    @field_validator("min_hourly", "min_fixed", "openai_base_url", mode="before")
    @classmethod
    def empty_str_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value


_cache: Settings | None = None
_overlay_enabled = False


def session_secret_path(data_dir: Path) -> Path:
    return data_dir / SESSION_SECRET_FILENAME


def resolve_session_secret(data_dir: Path, env_secret: str) -> str:
    path = session_secret_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    custom = env_secret.strip()
    if custom and custom != DEFAULT_SESSION_SECRET:
        if not path.exists():
            path.write_text(custom, encoding="utf-8")
        return custom
    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    generated = secrets.token_urlsafe(48)
    path.write_text(generated, encoding="utf-8")
    return generated


def enable_store_overlay() -> None:
    global _overlay_enabled
    _overlay_enabled = True


def _apply_overlay(base: Settings, overlay: dict[str, str]) -> Settings:
    values = dict(overlay)
    if "poll_interval_seconds" not in values and "poll_interval_minutes" in values:
        try:
            values["poll_interval_seconds"] = str(max(1, int(values["poll_interval_minutes"]) * 60))
        except ValueError:
            pass
    int_keys = {
        "poll_interval_seconds",
        "search_gap_seconds",
        "find_jobs_min_interval_seconds",
        "poll_cooldown_seconds",
        "approval_ttl_hours",
    }
    updates: dict[str, object] = {}
    for key, raw in values.items():
        if key not in Settings.model_fields:
            continue
        if key in int_keys:
            try:
                updates[key] = int(raw)
            except ValueError:
                continue
        elif key == "openai_base_url":
            updates[key] = raw.strip() or None
        else:
            updates[key] = raw
    if not updates:
        return base
    return base.model_copy(update=updates)


def _load_settings() -> Settings:
    base = Settings()
    data_dir = Path(base.data_dir)
    secret = resolve_session_secret(data_dir, base.session_secret)
    base = base.model_copy(update={"session_secret": secret})
    if not _overlay_enabled:
        return base
    from app.env_store import load_overlay_map

    overlay = load_overlay_map()
    if not overlay:
        return base
    return _apply_overlay(base, overlay)


def get_settings() -> Settings:
    global _cache
    if _cache is None:
        _cache = _load_settings()
    return _cache


def reload_settings() -> Settings:
    global _cache
    _cache = _load_settings()
    return _cache
