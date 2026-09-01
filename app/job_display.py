from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import quote

from app.db.models import Job, UpworkApplication
from app.models import InboxSort
from app.upwork.mcp_client import derive_client_stats, fold_search_client, public_job_url


class ClientFact(TypedDict):
    label: str
    value: str


class ClientHistoryItem(TypedDict):
    title: str
    detail: str


class ClientReviewCard(TypedDict):
    title: str
    reviewer: str
    rating: float | None
    comment: str


class JobAttachmentCard(TypedDict):
    filename: str
    url: str
    path: str
    text: str
    error: str
    local_url: str


class JobCard(TypedDict):
    id: int
    title: str
    score: int | None
    score_reason: str
    client_score: int | None
    client_score_reason: str
    price_label: str
    timezone: str
    location: str
    status: str
    applied: bool
    applied_status: str
    url: str
    description: str
    facts: list[ClientFact]
    open_contracts: list[ClientHistoryItem]
    closed_contracts: list[ClientHistoryItem]
    client_reviews: list[ClientReviewCard]
    attachments: list[JobAttachmentCard]
    local: bool
    created_at: datetime | None
    posted_ago: str
    posted_local: str
    posted_kind: str
    posted_at: datetime | None
    proposal_count: int | None
    interviewing: int | None
    has_draft: bool


def _job_has_draft(job: Job) -> bool:
    proposals = list(job.proposals or [])
    if not proposals:
        return False
    latest = sorted(proposals, key=lambda item: item.id)[-1]
    return bool((latest.edited_text or latest.draft_text or "").strip())


def _as_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {}


def parse_upwork_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def relative_ago(when: datetime, now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    stamp = when.astimezone(current.tzinfo)
    seconds = int((current - stamp).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 45:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _nested_posting(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    posting = data.get("marketplaceJobPosting") if isinstance(data.get("marketplaceJobPosting"), dict) else None
    if posting is None and isinstance(raw.get("marketplaceJobPosting"), dict):
        posting = raw["marketplaceJobPosting"]
    return posting if isinstance(posting, dict) else raw


def _job_activity(job: Job) -> dict[str, Any]:
    posting = _nested_posting(_as_dict(job.raw_json))
    activity = posting.get("activityStat") if isinstance(posting.get("activityStat"), dict) else {}
    job_act = activity.get("jobActivity") if isinstance(activity.get("jobActivity"), dict) else {}
    return job_act if isinstance(job_act, dict) else {}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(float(str(value)))
    except ValueError:
        return None


def _posted_at(job: Job, data: dict[str, Any]) -> datetime | None:
    raw = _as_dict(job.raw_json)
    posting = _nested_posting(raw)
    content = posting.get("content") if isinstance(posting.get("content"), dict) else {}
    for value in (
        data.get("published_date"),
        data.get("created_date"),
        raw.get("published_date"),
        raw.get("created_date"),
        posting.get("publishedDateTime"),
        posting.get("createdDateTime"),
        content.get("publishedDateTime"),
        content.get("createdDateTime"),
    ):
        parsed = parse_upwork_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, dict):
        for key in ("displayValue", "name", "title", "label"):
            found = value.get(key)
            if found:
                return _fmt(found)
        return ""
    if isinstance(value, list):
        return ", ".join(part for part in (_fmt(item) for item in value) if part)
    return str(value)


def _money(value: Any) -> str:
    text = _fmt(value).replace(",", "")
    if not text:
        return ""
    try:
        amount = float(text)
        if amount >= 1000:
            return f"${amount:,.0f}"
        return f"${amount:g}"
    except ValueError:
        return text if text.startswith("$") else f"${text}"


def _short_date(value: Any) -> str:
    text = _fmt(value)
    if "T" in text:
        return text.split("T", 1)[0]
    return text


def _history_items(rows: Any) -> list[ClientHistoryItem]:
    if not isinstance(rows, list):
        return []
    items: list[ClientHistoryItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _fmt(row.get("title")) or "Untitled contract"
        kind = _fmt(row.get("type") or row.get("status")).replace("_", " ").title()
        bits = [
            kind,
            f"{_fmt(row.get('feedback_score'))}★" if row.get("feedback_score") not in (None, "") else "",
            f"to client {_fmt(row.get('feedback_to_client_score'))}★"
            if row.get("feedback_to_client_score") not in (None, "")
            else "",
            _short_date(row.get("started")),
            _short_date(row.get("ended")),
        ]
        items.append({"title": title, "detail": " · ".join(part for part in bits if part)})
    return items


def _client_data(job: Job) -> dict[str, Any]:
    details = _as_dict(job.client_json)
    info = _as_dict(job.client_info)
    merged = fold_search_client(details, info)
    if info.get("verification_status") in (True, "VERIFIED", "verified"):
        merged["verified"] = True
    derive_client_stats(merged)
    return merged


def _review_cards(rows: Any) -> list[ClientReviewCard]:
    if not isinstance(rows, list):
        return []
    cards: list[ClientReviewCard] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cards.append(
            {
                "title": _fmt(row.get("title")),
                "reviewer": _fmt(row.get("reviewer")),
                "rating": float(row["rating"]) if isinstance(row.get("rating"), (int, float)) else None,
                "comment": _fmt(row.get("comment")),
            }
        )
    return cards


def _attachment_cards(job: Job, rows: Any) -> list[JobAttachmentCard]:
    if not isinstance(rows, list):
        return []
    cards: list[JobAttachmentCard] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        filename = _fmt(row.get("filename")) or "attachment"
        path = _fmt(row.get("path"))
        stored = Path(path).name if path else filename
        cards.append(
            {
                "filename": filename,
                "url": _fmt(row.get("url")),
                "path": path,
                "text": _fmt(row.get("text")),
                "error": _fmt(row.get("error")),
                "local_url": f"/jobs/{job.id}/files/{quote(stored)}" if path and job.id else "",
            }
        )
    return cards


def job_card(job: Job, applied_status: str = "") -> JobCard:
    data = _client_data(job)
    quals = data.get("preferred_qualifications") if isinstance(data.get("preferred_qualifications"), dict) else {}
    location_parts = [data.get("city"), data.get("state"), data.get("country")]
    location = ", ".join(str(part) for part in location_parts if part)
    timezone = job.timezone or _fmt(data.get("timezone"))
    activity = _job_activity(job)
    proposal_count = _as_int(data.get("proposal_count"))
    if proposal_count is None:
        proposal_count = _as_int(activity.get("applicationsCount"))
    if proposal_count is None:
        proposal_count = _as_int(activity.get("proposal_count"))
    interviewing = _as_int(data.get("interviewing"))
    if interviewing is None:
        interviewing = _as_int(activity.get("totalInvitedToInterview"))
    if interviewing is None:
        interviewing = _as_int(activity.get("interviewing"))
    posted = _posted_at(job, data)
    local_now = datetime.now().astimezone()
    if posted is not None:
        posted_kind = "Posted"
        posted_ago = relative_ago(posted, local_now)
        posted_local = posted.astimezone(local_now.tzinfo).strftime("%Y-%m-%d %H:%M")
    else:
        found = parse_upwork_datetime(job.created_at) if job.created_at else None
        posted_kind = "Found"
        posted_ago = relative_ago(found, local_now) if found else ""
        posted_local = found.astimezone(local_now.tzinfo).strftime("%Y-%m-%d %H:%M") if found else ""
    facts: list[ClientFact] = []
    mapping = [
        ("Company", data.get("company")),
        ("Rating", data.get("rating")),
        ("Reviews", data.get("reviews")),
        ("Hires", data.get("hires")),
        ("Jobs posted", data.get("posted_jobs")),
        ("Verified payment", "yes" if data.get("verified") is True else ("no" if data.get("verified") is False else "")),
        ("Total spent", _money(data.get("spend_total")) if data.get("spend_total") else ""),
        ("Hours billed", data.get("hours_total")),
        ("Active contracts", data.get("contracts_active")),
        ("Lifetime contracts", data.get("contracts_total")),
        ("Posted" if posted is not None else "Found in inbox", posted_ago),
        ("Proposals on this job", proposal_count if proposal_count is not None else ""),
        ("Avg bid", _money(data.get("avg_bid")) if data.get("avg_bid") else ""),
        ("Experience", str(data.get("experience_level") or "").replace("_", " ").title()),
        ("Duration", data.get("duration")),
        ("Hours / week", data.get("engagement")),
        ("Connects to apply", data.get("connects_cost")),
        ("Can apply", data.get("can_apply")),
        ("Member since", data.get("member_since")),
        ("Hire rate", f"{_fmt(data.get('hire_rate'))}%" if data.get("hire_rate") not in (None, "") else ""),
        ("Avg spent / hire", _money(data.get("avg_spend")) if data.get("avg_spend") not in (None, "") else ""),
        ("Avg hourly paid", _money(data.get("avg_hourly_paid")) if data.get("avg_hourly_paid") not in (None, "") else ""),
        ("Invites sent", data.get("invites_sent") if data.get("invites_sent") is not None else ""),
        ("Interviewing", interviewing if interviewing is not None else ""),
        ("Unanswered invites", data.get("unanswered_invites") if data.get("unanswered_invites") not in (None, 0) else ""),
        ("Hired on this job", data.get("hired_on_job") if data.get("hired_on_job") not in (None, 0) else ""),
        ("English", quals.get("english_proficiency") if quals else ""),
        ("Contractor type", quals.get("contractor_type") if quals else ""),
        ("Rising talent only", quals.get("rising_talent") if quals else ""),
    ]
    for label, value in mapping:
        text = _fmt(value)
        if text:
            facts.append({"label": label, "value": text})
    return {
        "id": job.id,
        "title": job.title,
        "score": job.score,
        "score_reason": job.score_reason or "",
        "client_score": job.client_score,
        "client_score_reason": job.client_score_reason or "",
        "price_label": job.price_label or job.budget or "—",
        "timezone": timezone or "—",
        "location": location,
        "status": job.status,
        "applied": bool(job.applied_on_upwork) or job.status == "submitted",
        "applied_status": applied_status,
        "url": public_job_url(job.url or job.upwork_id),
        "description": job.description or "",
        "facts": facts,
        "open_contracts": _history_items(data.get("open_contracts")),
        "closed_contracts": _history_items(data.get("closed_contracts")),
        "client_reviews": _review_cards(data.get("client_reviews")),
        "attachments": _attachment_cards(job, data.get("attachments")),
        "local": True,
        "created_at": job.created_at,
        "posted_ago": posted_ago,
        "posted_local": posted_local,
        "posted_kind": posted_kind,
        "posted_at": posted or parse_upwork_datetime(job.created_at),
        "proposal_count": proposal_count,
        "interviewing": interviewing,
        "has_draft": _job_has_draft(job),
    }


def application_card(row: UpworkApplication) -> JobCard:
    facts: list[ClientFact] = []
    if row.status:
        facts.append({"label": "Proposal status", "value": row.status})
    if row.rate:
        facts.append({"label": "Your bid", "value": row.rate})
    return {
        "id": 0,
        "title": row.title or "Untitled application",
        "score": None,
        "score_reason": "",
        "client_score": None,
        "client_score_reason": "",
        "price_label": row.rate or "Applied",
        "timezone": "—",
        "location": "",
        "status": row.status or "applied",
        "applied": True,
        "applied_status": row.status,
        "url": public_job_url(row.posting_id) if row.posting_id else "",
        "description": "",
        "facts": facts,
        "open_contracts": [],
        "closed_contracts": [],
        "client_reviews": [],
        "attachments": [],
        "local": False,
        "created_at": row.synced_at,
        "posted_ago": "",
        "posted_local": "",
        "posted_kind": "",
        "posted_at": parse_upwork_datetime(row.synced_at) if row.synced_at else None,
        "proposal_count": None,
        "interviewing": None,
        "has_draft": True,
    }


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def sort_job_cards(cards: list[JobCard], sort: InboxSort) -> list[JobCard]:
    def when(card: JobCard) -> datetime:
        return _aware(card.get("posted_at") or card.get("created_at"))

    def score_val(card: JobCard) -> int:
        score = card.get("score")
        return score if isinstance(score, int) else -1

    if sort == InboxSort.oldest:
        return sorted(cards, key=when)
    if sort == InboxSort.score:
        return sorted(cards, key=lambda card: (score_val(card), when(card)), reverse=True)
    if sort == InboxSort.score_low:
        def low_key(card: JobCard) -> tuple[int, datetime]:
            score = score_val(card)
            return (score if score >= 0 else 10_000, when(card))

        return sorted(cards, key=low_key)
    return sorted(cards, key=when, reverse=True)
