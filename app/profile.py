from pathlib import Path
import json

import yaml
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import UpworkProfile
from app.db.session import SessionLocal
from app.models import FreelancerProfile


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


def load_overlay(settings: Settings) -> FreelancerProfile:
    path = settings.profile_path
    if not path.exists():
        queries = [q.strip() for q in settings.search_queries.split(",") if q.strip()]
        return FreelancerProfile(search_queries=queries)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    profile = FreelancerProfile.model_validate(data)
    if not profile.search_queries:
        profile.search_queries = [q.strip() for q in settings.search_queries.split(",") if q.strip()]
    return profile


def save_overlay(profile: FreelancerProfile, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    path: Path = settings.profile_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = profile.model_dump(exclude={"upwork_overview"})
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
        upwork_overview=snapshot.overview,
    )
