from __future__ import annotations

import json
from typing import TypedDict

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models import ChatMessage, FeedbackLog, Job, MessageRoom, UpworkApplication
from app.models import FeedbackOutcome, JobStatus
from app.tools.memory import log_interaction_feedback
from app.upwork.mcp_client import job_ref_keys


class ApplicationRow(TypedDict):
    posting_id: str
    proposal_id: str
    title: str
    status: str
    rate: str
    viewed: str


class DerivedOutcome(BaseModel):
    outcome: FeedbackOutcome
    note: str = ""


CLIENT_OUTCOMES: frozenset[str] = frozenset(
    {
        FeedbackOutcome.hired.value,
        FeedbackOutcome.shortlisted.value,
        FeedbackOutcome.messaged.value,
        FeedbackOutcome.viewed.value,
        FeedbackOutcome.ignored.value,
        FeedbackOutcome.rejected.value,
    }
)
PROGRESS_RANK: dict[FeedbackOutcome, int] = {
    FeedbackOutcome.viewed: 1,
    FeedbackOutcome.messaged: 2,
    FeedbackOutcome.shortlisted: 3,
}
TERMINAL_OUTCOMES: frozenset[FeedbackOutcome] = frozenset(
    {
        FeedbackOutcome.hired,
        FeedbackOutcome.rejected,
        FeedbackOutcome.ignored,
    }
)


def index_applications(rows: list[UpworkApplication]) -> dict[str, UpworkApplication]:
    index: dict[str, UpworkApplication] = {}
    for row in rows:
        for key in job_ref_keys(row.posting_id):
            index[key] = row
        if row.proposal_id:
            index[row.proposal_id] = row
    return index


def application_for_job(
    job: Job,
    index: dict[str, UpworkApplication] | None = None,
    db: Session | None = None,
) -> UpworkApplication | None:
    mapping = index
    if mapping is None:
        if db is None:
            return None
        mapping = index_applications(db.query(UpworkApplication).all())
    for key in job_ref_keys(job.upwork_id):
        found = mapping.get(key)
        if found is not None:
            return found
    return None


def application_for_posting(db: Session, posting_id: str) -> UpworkApplication | None:
    keys = job_ref_keys(posting_id)
    for row in db.query(UpworkApplication).all():
        if job_ref_keys(row.posting_id) & keys:
            return row
    return None


def latest_client_outcome(db: Session, job_id: int) -> FeedbackOutcome | None:
    logs = (
        db.query(FeedbackLog)
        .filter(FeedbackLog.job_id == job_id, FeedbackLog.outcome.in_(CLIENT_OUTCOMES))
        .order_by(FeedbackLog.created_at.desc())
        .all()
    )
    for row in logs:
        try:
            return FeedbackOutcome(row.outcome)
        except ValueError:
            continue
    return None


def should_record(current: FeedbackOutcome | None, new: FeedbackOutcome) -> bool:
    if current is None:
        return True
    if current == new:
        return False
    if current == FeedbackOutcome.hired:
        return False
    if new == FeedbackOutcome.hired:
        return True
    if new in TERMINAL_OUTCOMES:
        if current == FeedbackOutcome.rejected and new == FeedbackOutcome.ignored:
            return False
        return True
    if current in TERMINAL_OUTCOMES:
        return False
    return PROGRESS_RANK.get(new, 0) > PROGRESS_RANK.get(current, 0)


def outcome_from_status(status: str, viewed: bool) -> DerivedOutcome | None:
    lowered = status.lower().strip()
    if not lowered and not viewed:
        return None
    if "withdrawn" in lowered:
        return None
    if "hired" in lowered:
        return DerivedOutcome(outcome=FeedbackOutcome.hired, note=f"Upwork proposal status: {status}")
    if "declined" in lowered or lowered == "rejected":
        return DerivedOutcome(outcome=FeedbackOutcome.rejected, note=f"Upwork proposal status: {status}")
    if "offer" in lowered:
        return DerivedOutcome(outcome=FeedbackOutcome.shortlisted, note=f"Upwork proposal status: {status}")
    if "invitation" in lowered or lowered == "activated":
        return DerivedOutcome(outcome=FeedbackOutcome.shortlisted, note=f"Upwork proposal status: {status}")
    if "interview" in lowered:
        return DerivedOutcome(outcome=FeedbackOutcome.messaged, note=f"Upwork proposal status: {status}")
    if lowered == "archived":
        return DerivedOutcome(outcome=FeedbackOutcome.ignored, note=f"Upwork proposal status: {status}")
    if viewed:
        return DerivedOutcome(outcome=FeedbackOutcome.viewed, note="Upwork: proposal marked viewed")
    return None


def job_id_for_room(db: Session, room: MessageRoom, *, fuzzy: bool = True) -> int | None:
    if room.context_id:
        keys = job_ref_keys(room.context_id)
        for job in db.query(Job).all():
            if job_ref_keys(job.upwork_id) & keys:
                return job.id
        app = (
            db.query(UpworkApplication)
            .filter(UpworkApplication.proposal_id == room.context_id)
            .one_or_none()
        )
        if app is not None:
            matched = _job_for_keys(db, job_ref_keys(app.posting_id))
            if matched is not None:
                return matched.id
    if not fuzzy:
        return None
    return _fuzzy_related_job(db, room)


def _job_for_keys(db: Session, keys: set[str]) -> Job | None:
    if not keys:
        return None
    for job in db.query(Job).all():
        if job_ref_keys(job.upwork_id) & keys:
            return job
    return None


def _company_name(job: Job) -> str:
    try:
        data = json.loads(job.client_json or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("company", "companyName", "client_name", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _hired_on_job(job: Job) -> int:
    try:
        data = json.loads(job.client_json or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0
    raw = data.get("hired_on_job")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _fuzzy_related_job(db: Session, room: MessageRoom) -> int | None:
    title = (room.title or "").lower()
    if len(title) < 4:
        return None
    applied = (
        db.query(Job)
        .filter((Job.applied_on_upwork.is_(True)) | (Job.status == JobStatus.submitted.value))
        .all()
    )
    hits: list[Job] = []
    for job in applied:
        company = _company_name(job).lower()
        if len(company) >= 4 and company in title:
            hits.append(job)
            continue
        job_title = (job.title or "").lower()
        if len(job_title) >= 12 and (job_title in title or title in job_title):
            hits.append(job)
    if len(hits) == 1:
        return hits[0].id
    return None


def _job_index(db: Session) -> dict[str, Job]:
    index: dict[str, Job] = {}
    for job in db.query(Job).all():
        for key in job_ref_keys(job.upwork_id):
            index[key] = job
    return index


def _job_from_posting(posting_id: str, index: dict[str, Job]) -> Job | None:
    for key in job_ref_keys(posting_id):
        found = index.get(key)
        if found is not None:
            return found
    return None


def _jobs_with_client_messages(db: Session) -> dict[int, str]:
    found: dict[int, str] = {}
    rooms = db.query(MessageRoom).all()
    for room in rooms:
        job_id = job_id_for_room(db, room, fuzzy=(room.context_type or "").lower() == "interview")
        if job_id is None:
            continue
        has_client = (
            db.query(ChatMessage.id)
            .filter(ChatMessage.room_pk == room.id, ChatMessage.sender == "client")
            .first()
            is not None
        )
        room_type = (room.context_type or "").lower()
        if not has_client and room_type not in {"interview", "proposal"}:
            continue
        label = room.context_type or "room"
        found[job_id] = f"Upwork: client messaged ({label}: {(room.title or '')[:80]})"
    return found


async def apply_upwork_outcomes(db: Session, rows: list[ApplicationRow] | None = None) -> int:
    viewed_by_posting: dict[str, bool] = {}
    for row in rows or []:
        posting_id = row.get("posting_id")
        if not posting_id:
            continue
        flag = row.get("viewed") == "1"
        for key in job_ref_keys(posting_id):
            viewed_by_posting[key] = viewed_by_posting.get(key, False) or flag
    jobs = _job_index(db)
    pending: dict[int, DerivedOutcome] = {}
    for app in db.query(UpworkApplication).all():
        job = _job_from_posting(app.posting_id, jobs)
        if job is None:
            continue
        viewed = any(viewed_by_posting.get(key) for key in job_ref_keys(app.posting_id))
        derived = outcome_from_status(app.status, viewed)
        if derived is not None:
            pending[job.id] = derived
    for job_id, note in _jobs_with_client_messages(db).items():
        current = pending.get(job_id)
        candidate = DerivedOutcome(outcome=FeedbackOutcome.messaged, note=note)
        if current is None or should_record(current.outcome, candidate.outcome):
            pending[job_id] = candidate
        elif current.outcome == candidate.outcome:
            pending[job_id] = candidate
    for job in db.query(Job).filter(
        (Job.applied_on_upwork.is_(True)) | (Job.status == JobStatus.submitted.value)
    ).all():
        app = application_for_job(job, db=db)
        status = (app.status or "").lower() if app else ""
        if "hired" in status or "offer" in status:
            continue
        if _hired_on_job(job) <= 0:
            continue
        current = pending.get(job.id)
        if current is not None and current.outcome in {FeedbackOutcome.hired, FeedbackOutcome.rejected}:
            continue
        candidate = DerivedOutcome(
            outcome=FeedbackOutcome.ignored,
            note="Upwork: job hired someone else",
        )
        if current is None or should_record(current.outcome, candidate.outcome):
            pending[job.id] = candidate
    logged = 0
    for job_id, derived in pending.items():
        job = db.query(Job).filter(Job.id == job_id).one_or_none()
        if job is None:
            continue
        latest = latest_client_outcome(db, job.id)
        if not should_record(latest, derived.outcome):
            continue
        await log_interaction_feedback(str(job.id), derived.outcome.value, derived.note, db=db)
        logged += 1
    return logged
