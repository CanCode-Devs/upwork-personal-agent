from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dashboard_username: str = "admin"
    dashboard_password: str = "change-me"
    session_secret: str = "replace-with-a-long-random-string"

    openai_api_key: str = ""
    openai_base_url: str | None = None
    openai_model: str = "gpt-4o-mini"

    app_name: str = "Upwork Personal Agent"
    app_tagline: str = "Adaptive agent"
    seed_demo_portfolio: bool = False
    scoring_path: Path = Field(default=Path("./profiles/scoring.yaml"))

    search_queries: str = "python fastapi"
    min_score: int = 70
    poll_interval_minutes: int = 15
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


def get_settings() -> Settings:
    return Settings()
