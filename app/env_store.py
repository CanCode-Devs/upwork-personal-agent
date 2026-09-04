from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings, reload_settings
from app.db.models import AppConfig, utcnow
from app.db.session import SessionLocal
from app.profile import load_profile
from app.search_queries import clamped_poll_interval_seconds, min_poll_interval_seconds, poll_queries

OWNER_KEY = "dashboard_username"
SOURCE_SAVED = "saved"
SOURCE_ENVIRONMENT = "environment"
SOURCE_UNSET = "unset"


class CatalogField(BaseModel):
    key: str
    label: str
    help: str
    secret: bool = False
    value_type: Literal["str", "optional_str", "int"] = "str"
    note: str = ""


SECRET_FIELDS: tuple[CatalogField, ...] = (
    CatalogField(
        key="openai_api_key",
        label="OpenAI API key",
        help="Required to score jobs, suggest queries, and write proposals.",
        secret=True,
    ),
)

VARIABLE_FIELDS: tuple[CatalogField, ...] = (
    CatalogField(
        key="openai_base_url",
        label="OpenAI base URL",
        help="Leave blank for the default OpenAI API. Set this for a compatible proxy.",
        value_type="optional_str",
    ),
    CatalogField(
        key="openai_model",
        label="Chat model",
        help="Used for scoring, search-query suggestions, and chat replies.",
    ),
    CatalogField(
        key="openai_draft_model",
        label="Draft model",
        help="Used for cover letters, screening answers, and milestones.",
    ),
    CatalogField(
        key="app_name",
        label="App name",
        help="Shown in the dashboard, page titles, and the Upwork OAuth client name.",
    ),
    CatalogField(
        key="app_tagline",
        label="App tagline",
        help="Short line under the app name in the sidebar.",
    ),
    CatalogField(
        key="search_gap_seconds",
        label="Search gap (seconds)",
        help="Wait after each title or keyword search before the next one. Extra pages of the same search do not use this gap.",
        value_type="int",
    ),
    CatalogField(
        key="find_jobs_min_interval_seconds",
        label="Find-jobs min interval (seconds)",
        help="Minimum space between every Upwork find_jobs call, including extra pages and job-detail fetches.",
        value_type="int",
    ),
    CatalogField(
        key="poll_cooldown_seconds",
        label="Poll cooldown (seconds)",
        help="After a full sweep finishes, wait this long before the next automatic poll. Poll now skips this wait.",
        value_type="int",
    ),
    CatalogField(
        key="poll_interval_seconds",
        label="Poll interval (seconds)",
        help="Target time from one poll start to the next. Cannot go below searches × search gap + cooldown.",
        value_type="int",
    ),
    CatalogField(
        key="approval_ttl_hours",
        label="Approval window (hours)",
        help="How long a pending job stays in Inbox before it expires.",
        value_type="int",
    ),
    CatalogField(
        key="embedding_model",
        label="Embedding model",
        help="Local sentence-transformers checkpoint for scoring and example matching.",
        note="A new model may need a restart so warmup can download the files.",
    ),
    CatalogField(
        key="upwork_mcp_url",
        label="Upwork MCP URL",
        help="Official Upwork MCP endpoint. Leave the default unless Upwork documents a new URL.",
    ),
)

SECRET_KEYS = frozenset(field.key for field in SECRET_FIELDS)
VARIABLE_KEYS = frozenset(field.key for field in VARIABLE_FIELDS)
CATALOG_KEYS = SECRET_KEYS | VARIABLE_KEYS


class SecretRow(TypedDict):
    key: str
    label: str
    help: str
    is_set: bool
    stored: bool
    source: str


class VariableRow(TypedDict):
    key: str
    label: str
    help: str
    value: str
    note: str
    value_type: str


class ConfigPageView(TypedDict):
    secrets: list[SecretRow]
    variables: list[VariableRow]
    openai_key_missing: bool


class SecretWrite(BaseModel):
    key: str
    value: str = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def known_secret(cls, value: str) -> str:
        if value not in SECRET_KEYS:
            raise ValueError("unknown")
        return value

    @field_validator("value")
    @classmethod
    def strip_secret(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("required")
        return text


class VariablesWrite(BaseModel):
    openai_base_url: str = ""
    openai_model: str = Field(min_length=1)
    openai_draft_model: str = Field(min_length=1)
    app_name: str = Field(min_length=1)
    app_tagline: str = Field(min_length=1)
    search_gap_seconds: int = Field(ge=1)
    find_jobs_min_interval_seconds: int = Field(ge=1)
    poll_cooldown_seconds: int = Field(ge=1)
    poll_interval_seconds: int = Field(ge=1)
    approval_ttl_hours: int = Field(ge=1)
    embedding_model: str = Field(min_length=1)
    upwork_mcp_url: str = Field(min_length=1)

    @field_validator(
        "openai_base_url",
        "openai_model",
        "openai_draft_model",
        "app_name",
        "app_tagline",
        "embedding_model",
        "upwork_mcp_url",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


def load_overlay_map() -> dict[str, str]:
    try:
        db = SessionLocal()
    except Exception:
        return {}
    try:
        rows = db.query(AppConfig).all()
    except (OperationalError, ProgrammingError):
        return {}
    except Exception:
        return {}
    finally:
        db.close()
    return {row.key: row.value for row in rows}


def _upsert(db: Session, key: str, value: str, is_secret: bool) -> None:
    row = db.query(AppConfig).filter(AppConfig.key == key).one_or_none()
    if row is None:
        db.add(
            AppConfig(
                key=key,
                value=value,
                is_secret=is_secret,
                updated_at=utcnow(),
            )
        )
        return
    row.value = value
    row.is_secret = is_secret
    row.updated_at = utcnow()


def _migrate_poll_interval_minutes(db: Session, existing: set[str], env: Settings) -> set[str]:
    row = db.query(AppConfig).filter(AppConfig.key == "poll_interval_minutes").one_or_none()
    if row is None:
        return existing
    keys = set(existing)
    if "poll_interval_seconds" not in keys:
        try:
            seconds = str(max(1, int(row.value) * 60))
        except (TypeError, ValueError):
            seconds = str(env.poll_interval_seconds)
        _upsert(db, "poll_interval_seconds", seconds, is_secret=False)
        keys.add("poll_interval_seconds")
    db.delete(row)
    keys.discard("poll_interval_minutes")
    db.flush()
    return keys


def seed_from_env(db: Session, env: Settings) -> None:
    existing = {row.key for row in db.query(AppConfig).all()}
    existing = _migrate_poll_interval_minutes(db, existing, env)
    for field in SECRET_FIELDS:
        if field.key in existing:
            continue
        value = getattr(env, field.key) or ""
        if not str(value).strip():
            continue
        db.add(
            AppConfig(
                key=field.key,
                value=str(value),
                is_secret=True,
                updated_at=utcnow(),
            )
        )
    for field in VARIABLE_FIELDS:
        if field.key in existing:
            continue
        raw = getattr(env, field.key)
        stored = "" if raw is None else str(raw)
        db.add(
            AppConfig(
                key=field.key,
                value=stored,
                is_secret=False,
                updated_at=utcnow(),
            )
        )
    if OWNER_KEY not in existing:
        db.add(
            AppConfig(
                key=OWNER_KEY,
                value=env.dashboard_username,
                is_secret=False,
                updated_at=utcnow(),
            )
        )
    db.flush()


def set_owner_username(db: Session, username: str) -> None:
    _upsert(db, OWNER_KEY, username.strip(), is_secret=False)
    db.flush()


def save_secret(db: Session, payload: SecretWrite) -> None:
    _upsert(db, payload.key, payload.value, is_secret=True)
    db.flush()


def delete_secret(db: Session, key: str) -> None:
    if key not in SECRET_KEYS:
        raise ValueError("unknown")
    row = db.query(AppConfig).filter(AppConfig.key == key).one_or_none()
    if row is not None:
        db.delete(row)
        db.flush()


def save_variables(db: Session, payload: VariablesWrite) -> None:
    values = payload.model_dump()
    settings = get_settings()
    profile = load_profile(settings, db=db)
    values["poll_interval_seconds"] = clamped_poll_interval_seconds(
        values["poll_interval_seconds"],
        len(poll_queries(profile, settings)),
        values["search_gap_seconds"],
        values["poll_cooldown_seconds"],
    )
    for field in VARIABLE_FIELDS:
        raw = values[field.key]
        stored = "" if raw is None else str(raw)
        _upsert(db, field.key, stored, is_secret=False)
    db.flush()


def ensure_poll_interval_floor(db: Session, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    profile = load_profile(settings, db=db)
    floor = min_poll_interval_seconds(
        len(poll_queries(profile, settings)),
        settings.search_gap_seconds,
        settings.poll_cooldown_seconds,
    )
    if settings.poll_interval_seconds >= floor:
        return False
    _upsert(db, "poll_interval_seconds", str(floor), is_secret=False)
    db.flush()
    return True


def _secret_row(field: CatalogField, overlay: dict[str, str], resolved: Settings) -> SecretRow:
    stored = field.key in overlay
    resolved_value = str(getattr(resolved, field.key) or "").strip()
    is_set = bool(resolved_value)
    if stored:
        source = SOURCE_SAVED
    elif is_set:
        source = SOURCE_ENVIRONMENT
    else:
        source = SOURCE_UNSET
    return SecretRow(
        key=field.key,
        label=field.label,
        help=field.help,
        is_set=is_set,
        stored=stored,
        source=source,
    )


def _variable_row(field: CatalogField, resolved: Settings) -> VariableRow:
    raw = getattr(resolved, field.key)
    value = "" if raw is None else str(raw)
    return VariableRow(
        key=field.key,
        label=field.label,
        help=field.help,
        value=value,
        note=field.note,
        value_type=field.value_type,
    )


def config_page_view(resolved: Settings) -> ConfigPageView:
    overlay = load_overlay_map()
    secrets = [_secret_row(field, overlay, resolved) for field in SECRET_FIELDS]
    profile = load_profile(resolved)
    query_count = len(poll_queries(profile, resolved))
    floor = min_poll_interval_seconds(
        query_count,
        resolved.search_gap_seconds,
        resolved.poll_cooldown_seconds,
    )
    variables: list[VariableRow] = []
    for field in VARIABLE_FIELDS:
        row = _variable_row(field, resolved)
        if field.key == "poll_interval_seconds":
            row["note"] = (
                f"Current floor is {floor}s "
                f"({query_count} searches × {resolved.search_gap_seconds}s gap "
                f"+ {resolved.poll_cooldown_seconds}s cooldown)."
            )
        variables.append(row)
    return ConfigPageView(
        secrets=secrets,
        variables=variables,
        openai_key_missing=not bool((resolved.openai_api_key or "").strip()),
    )


def persist_and_reload(db: Session) -> Settings:
    db.commit()
    return reload_settings()
