import json

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Job, Proposal
from app.embeddings import add_embedding
from app.events import add_event
from app.milestones import dump_milestones
from app.models import EmbeddingSource, FeedbackOutcome, JobStatus, ProposalMilestone, ScreeningAnswer
from app.profile import load_overlay
from app.proposal_settings import load_proposal_settings
from app.proposal_writer import (
    UnprovenAnswersError,
    dump_apply,
    dump_screening,
    extract_apply_questions,
    finalize_letter,
    load_apply,
    unproven_answer_gaps,
)
from app.tools.execution import bid_amount_for_job, cap_highlight_picks, job_is_fixed, quote_amount_for_job, submit_proposal
from app.tools.memory import log_interaction_feedback
from app.upwork.mcp_client import UpworkMcpClient


def latest_proposal(job: Job) -> Proposal | None:
    if not job.proposals:
        return None
    return sorted(job.proposals, key=lambda item: item.id)[-1]


def cover_letter_for(job: Job, db: Session | None = None) -> str:
    proposal = latest_proposal(job)
    if proposal is None:
        return ""
    writer = load_proposal_settings(db)
    return finalize_letter(
        (proposal.edited_text or proposal.draft_text or "").strip(),
        hook=writer.opening_hook,
        enforce=writer.enforce_opening_hook,
    )


async def approve_and_submit(
    db: Session,
    job: Job,
    client: UpworkMcpClient | None = None,
    cover_letter: str | None = None,
    boost_connects: int = 0,
    milestones: list[ProposalMilestone] | None = None,
    screening_answers: list[ScreeningAnswer] | None = None,
    portfolio_project_ids: list[str] | None = None,
    certificate_ids: list[str] | None = None,
    job_history_ids: list[str] | None = None,
    profile_history_ids: list[str] | None = None,
    attachment_uids: list[str] | None = None,
    user_id: int | None = None,
    charged_amount: float | None = None,
) -> Job:
    if job.applied_on_upwork:
        raise ValueError("Already applied on Upwork")
    if job.status not in {JobStatus.pending_review.value, JobStatus.submit_failed.value}:
        raise ValueError(f"Job cannot be approved from status {job.status}")
    letter = (cover_letter or "").strip() or cover_letter_for(job)
    if not letter:
        raise ValueError("Cover letter is empty")
    if screening_answers:
        missing = [item.question for item in screening_answers if not item.answer.strip()]
        if missing:
            raise ValueError("Answer every screening question before submit")
    if cover_letter is not None:
        save_edit(
            db,
            job,
            letter,
            milestones,
            screening_answers=screening_answers,
            portfolio_project_ids=portfolio_project_ids,
            certificate_ids=certificate_ids,
            job_history_ids=job_history_ids,
            profile_history_ids=profile_history_ids,
            user_id=user_id,
            charged_amount=charged_amount,
        )
    apply_questions = extract_apply_questions(job.description or "")
    try:
        details = json.loads(job.client_json or "{}")
    except json.JSONDecodeError:
        details = {}
    if isinstance(details, dict) and str(details.get("attachment_text") or "").strip():
        for item in extract_apply_questions(str(details.get("attachment_text") or "")):
            if item not in apply_questions:
                apply_questions.append(item)
    gaps = unproven_answer_gaps(letter, screening_answers or [], apply_questions)
    if gaps:
        raise UnprovenAnswersError(gaps)
    overlay = load_overlay(get_settings())
    stored = None
    raw_stored = load_apply(latest_proposal(job)).get("charged_amount")
    if isinstance(raw_stored, (int, float)) and float(raw_stored) > 0:
        stored = float(raw_stored)
    if job_is_fixed(job):
        bid = bid_amount_for_job(job, overlay.hourly_rate)
    else:
        bid = quote_amount_for_job(job, overlay.hourly_rate, charged_amount if charged_amount else stored)
    job.status = JobStatus.approved.value
    add_event(
        db,
        "approved",
        f"Approved from dashboard boost={max(0, boost_connects)} milestones={len(milestones or [])}",
        job.id,
        user_id=user_id,
    )
    await log_interaction_feedback(str(job.id), FeedbackOutcome.approved.value, "Approved from dashboard", db=db)
    result = await submit_proposal(
        str(job.id),
        letter,
        db=db,
        boost_connects=max(0, boost_connects),
        charged_amount=bid,
        milestones=milestones,
        screening_answers=screening_answers,
        portfolio_project_ids=portfolio_project_ids,
        certificate_ids=certificate_ids,
        attachment_uids=attachment_uids,
    )
    db.refresh(job)
    _ = client
    _ = result
    return job


async def reject_job(db: Session, job: Job, reason: str = "", user_id: int | None = None) -> Job:
    if job.status not in {JobStatus.pending_review.value, JobStatus.submit_failed.value}:
        raise ValueError(f"Job cannot be rejected from status {job.status}")
    job.status = JobStatus.rejected.value
    note = reason.strip() or "Rejected from dashboard"
    add_event(db, "rejected", note, job.id, user_id=user_id)
    await log_interaction_feedback(str(job.id), FeedbackOutcome.rejected.value, note, db=db)
    return job


def save_hourly_quote(
    db: Session,
    job: Job,
    amount: float,
    user_id: int | None = None,
) -> Proposal:
    if job_is_fixed(job):
        raise ValueError("Fixed-price quotes are not editable")
    if amount <= 0:
        raise ValueError("Hourly quote must be greater than 0")
    proposal = latest_proposal(job)
    if proposal is None:
        proposal = Proposal(job_id=job.id, draft_text="", edited_text="")
        db.add(proposal)
        db.flush()
    raw = load_apply(proposal)
    raw["charged_amount"] = int(amount) if float(amount).is_integer() else amount
    proposal.apply_json = dump_apply(raw)
    db.flush()
    add_event(db, "edited", f"Hourly quote set to ${raw['charged_amount']}/hr", job.id, user_id=user_id)
    return proposal


def save_edit(
    db: Session,
    job: Job,
    cover_letter: str,
    milestones: list[ProposalMilestone] | None = None,
    screening_answers: list[ScreeningAnswer] | None = None,
    portfolio_project_ids: list[str] | None = None,
    certificate_ids: list[str] | None = None,
    job_history_ids: list[str] | None = None,
    profile_history_ids: list[str] | None = None,
    user_id: int | None = None,
    charged_amount: float | None = None,
) -> Proposal:
    writer = load_proposal_settings(db)
    letter = finalize_letter(cover_letter, hook=writer.opening_hook, enforce=writer.enforce_opening_hook)
    proposal = latest_proposal(job)
    if proposal is None:
        proposal = Proposal(job_id=job.id, draft_text=letter, edited_text=letter)
        db.add(proposal)
        db.flush()
    else:
        proposal.edited_text = letter
    if milestones is not None:
        proposal.milestones_json = dump_milestones(milestones)
    if screening_answers is not None:
        proposal.screening_json = dump_screening(screening_answers)
    if (
        portfolio_project_ids is not None
        or certificate_ids is not None
        or job_history_ids is not None
        or profile_history_ids is not None
        or charged_amount is not None
    ):
        raw = load_apply(proposal)
        if portfolio_project_ids is not None:
            raw["portfolio_project_ids"] = portfolio_project_ids
        if certificate_ids is not None:
            raw["certificate_ids"] = certificate_ids
        if job_history_ids is not None:
            raw["job_history_ids"] = job_history_ids
        if profile_history_ids is not None:
            raw["profile_history_ids"] = profile_history_ids
        if charged_amount is not None and charged_amount > 0:
            raw["charged_amount"] = charged_amount
        capped = cap_highlight_picks(
            portfolio_project_ids=list(raw.get("portfolio_project_ids") or []),
            certificate_ids=list(raw.get("certificate_ids") or []),
            job_history_ids=list(raw.get("job_history_ids") or []),
            profile_history_ids=list(raw.get("profile_history_ids") or []),
        )
        raw.update(capped)
        proposal.apply_json = dump_apply(raw)
    db.flush()
    add_embedding(db, EmbeddingSource.proposal, proposal.id, cover_letter)
    add_event(db, "edited", "Cover letter updated", job.id, user_id=user_id)
    return proposal
