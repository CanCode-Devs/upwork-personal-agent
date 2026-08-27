from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import AppRuntimeSettings, PortfolioItem
from app.llm import llm_suggest_search_queries
from app.models import (
    FreelancerProfile,
    SearchQueryContext,
    SearchQueryProfile,
    WorkHistorySnippet,
    WorkKind,
    WorkOrigin,
)
from app.profile import load_profile
from app.runtime import get_or_create_runtime

_DESC_LIMIT = 400
_ITEM_LIMIT = 30


def normalize_query(text: str) -> str:
    return " ".join(text.lower().split())


def _parse_str_list(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        text = " ".join(str(item or "").split())
        key = normalize_query(text)
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _dump_str_list(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False)


def pending_queries(row: AppRuntimeSettings) -> list[str]:
    return _parse_str_list(getattr(row, "pending_search_queries", None))


def dismissed_queries(row: AppRuntimeSettings) -> list[str]:
    return _parse_str_list(getattr(row, "dismissed_search_queries", None))


def set_pending_queries(row: AppRuntimeSettings, items: list[str]) -> None:
    row.pending_search_queries = _dump_str_list(items)


def set_dismissed_queries(row: AppRuntimeSettings, items: list[str]) -> None:
    row.dismissed_search_queries = _dump_str_list(items)


def _json_list_field(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [part.strip() for part in parsed.split(",") if part.strip()]
    return []


def _snippet(row: PortfolioItem) -> WorkHistorySnippet:
    description = (row.description or "").strip()
    if len(description) > _DESC_LIMIT:
        description = description[:_DESC_LIMIT].rstrip() + "…"
    outcomes = (row.outcomes_achieved or "").strip()
    if len(outcomes) > _DESC_LIMIT:
        outcomes = outcomes[:_DESC_LIMIT].rstrip() + "…"
    return {
        "origin": row.origin or WorkOrigin.agent.value,
        "kind": row.kind or WorkKind.project.value,
        "title": (row.project_title or "").strip(),
        "tech": _json_list_field(row.tech_stack),
        "outcomes": outcomes,
        "keywords": _json_list_field(row.associated_keywords),
        "description": description,
    }


def work_history_for_prompt(db: Session) -> tuple[list[WorkHistorySnippet], list[WorkHistorySnippet]]:
    rows = (
        db.query(PortfolioItem)
        .filter(PortfolioItem.kind != WorkKind.proposal.value)
        .order_by(PortfolioItem.created_at.desc())
        .all()
    )
    upwork: list[WorkHistorySnippet] = []
    agent: list[WorkHistorySnippet] = []
    for row in rows:
        snippet = _snippet(row)
        if not snippet["title"] and not snippet["description"]:
            continue
        if row.origin == WorkOrigin.upwork.value:
            if len(upwork) < _ITEM_LIMIT:
                upwork.append(snippet)
        elif len(agent) < _ITEM_LIMIT:
            agent.append(snippet)
    return upwork, agent


def build_search_context(profile: FreelancerProfile, db: Session) -> SearchQueryContext:
    upwork, agent = work_history_for_prompt(db)
    packed: SearchQueryProfile = {
        "name": profile.name,
        "title": profile.title,
        "skills": list(profile.skills),
        "hourly_rate": profile.hourly_rate,
        "voice": profile.voice,
        "exclude_keywords": list(profile.exclude_keywords),
        "current_queries": list(profile.search_queries),
        "upwork_overview": (profile.upwork_overview or "")[:2000],
    }
    return {"profile": packed, "upwork_history": upwork, "agent_history": agent}


def _blocked_keys(profile: FreelancerProfile, dismissed: list[str]) -> set[str]:
    keys = {normalize_query(item) for item in profile.search_queries}
    keys.update(normalize_query(item) for item in dismissed)
    keys.update(normalize_query(item) for item in profile.exclude_keywords)
    return {item for item in keys if item}


def filter_suggestions(
    queries: list[str],
    profile: FreelancerProfile,
    dismissed: list[str],
) -> list[str]:
    blocked = _blocked_keys(profile, dismissed)
    exclude = [item.lower() for item in profile.exclude_keywords if item.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for item in queries:
        text = " ".join(item.split())
        key = normalize_query(text)
        if not text or key in seen or key in blocked:
            continue
        lowered = text.lower()
        if any(token in lowered for token in exclude):
            continue
        seen.add(key)
        out.append(text)
    return out


def suggest_search_queries(
    db: Session,
    settings: Settings | None = None,
) -> list[str]:
    settings = settings or get_settings()
    profile = load_profile(settings, db=db)
    row = get_or_create_runtime(db, settings)
    dismissed = dismissed_queries(row)
    context = build_search_context(profile, db)
    raw = llm_suggest_search_queries(context, settings)
    pending = filter_suggestions(raw, profile, dismissed)
    set_pending_queries(row, pending)
    return pending


def accept_search_query(overlay: FreelancerProfile, query: str) -> FreelancerProfile:
    text = " ".join(query.split())
    if not text:
        return overlay
    key = normalize_query(text)
    existing = [item for item in overlay.search_queries if normalize_query(item) != key]
    overlay.search_queries = [*existing, text]
    return overlay


def remove_pending_query(row: AppRuntimeSettings, query: str) -> None:
    key = normalize_query(query)
    remaining = [item for item in pending_queries(row) if normalize_query(item) != key]
    set_pending_queries(row, remaining)


def dismiss_search_query(row: AppRuntimeSettings, query: str) -> None:
    text = " ".join(query.split())
    if not text:
        return
    remove_pending_query(row, text)
    dismissed = dismissed_queries(row)
    key = normalize_query(text)
    kept = [item for item in dismissed if normalize_query(item) != key]
    set_dismissed_queries(row, [*kept, text])
