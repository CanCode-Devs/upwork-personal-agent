from pathlib import Path
import json

import yaml
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import UpworkProfile
from app.db.session import SessionLocal
from app.models import FreelancerProfile

_EXAMPLE_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "example.yaml"


def ensure_profile_file(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path: Path = settings.profile_path
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    source = _EXAMPLE_PROFILE
    if not source.exists():
        source = Path("profiles/example.yaml")
    if source.exists():
        path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _skills(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _snapshot_skills(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return _skills(parsed)


def unique_terms(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item or "").split())
        key = " ".join(text.lower().split())
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def config_search_queries(settings: Settings) -> list[str]:
    return unique_terms([part.strip() for part in settings.search_queries.split(",") if part.strip()])


def sync_search_fields(profile: FreelancerProfile) -> FreelancerProfile:
    profile.job_titles = unique_terms(profile.job_titles)
    profile.title_keywords = unique_terms(profile.title_keywords)
    profile.title_exclude_keywords = unique_terms(profile.title_exclude_keywords)
    profile.search_queries = unique_terms([*profile.job_titles, *profile.title_keywords])
    profile.exclude_keywords = list(profile.title_exclude_keywords)
    return profile


def apply_search_field_fallbacks(profile: FreelancerProfile, settings: Settings) -> FreelancerProfile:
    if not profile.job_titles and not profile.title_keywords:
        profile.title_keywords = unique_terms(profile.search_queries) or config_search_queries(settings)
    if not profile.title_exclude_keywords:
        profile.title_exclude_keywords = unique_terms(profile.exclude_keywords)
    return sync_search_fields(profile)


def load_overlay(settings: Settings) -> FreelancerProfile:
    ensure_profile_file(settings)
    path = settings.profile_path
    if not path.exists():
        queries = config_search_queries(settings)
        return apply_search_field_fallbacks(
            FreelancerProfile(search_queries=queries, title_keywords=queries),
            settings,
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    profile = FreelancerProfile.model_validate(data)
    return apply_search_field_fallbacks(profile, settings)


def save_overlay(profile: FreelancerProfile, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    path: Path = settings.profile_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = sync_search_fields(profile).model_dump(exclude={"upwork_overview"})
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_profile(settings: Settings | None = None, db: Session | None = None) -> FreelancerProfile:
    settings = settings or get_settings()
    overlay = load_overlay(settings)
    own = False
    session = db
    if session is None:
        session = SessionLocal()
        own = True
    try:
        snapshot = session.query(UpworkProfile).order_by(UpworkProfile.id.asc()).first()
    finally:
        if own:
            session.close()
    if snapshot is None:
        return overlay
    skills = list(dict.fromkeys([*overlay.skills, *_snapshot_skills(snapshot.skills_json)]))
    return FreelancerProfile(
        name=overlay.name,
        title=overlay.title or snapshot.title,
        skills=skills,
        hourly_rate=overlay.hourly_rate if overlay.hourly_rate is not None else snapshot.hourly_rate,
        voice=overlay.voice,
        working_hours=overlay.working_hours,
        exclude_keywords=overlay.exclude_keywords,
        search_queries=overlay.search_queries,
        job_titles=overlay.job_titles,
        title_keywords=overlay.title_keywords,
        title_exclude_keywords=overlay.title_exclude_keywords,
        upwork_overview=snapshot.overview,
    )
