from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db.models import Job, PreferenceRule, Proposal
from app.db.session import SessionLocal
from app.embeddings import cosine, embed_texts
from app.llm import llm_extract_style_rules
from app.models import FeedbackOutcome

logger = logging.getLogger(__name__)

LEARNED_LEVEL = "learned"
STYLE_CATEGORY = "proposal_style"
MAX_LEARNED_RULES = 20
DEDUPE_THRESHOLD = 0.86
MIN_EDIT_CHARS = 40
_GENERIC_REJECT = {"rejected from dashboard", "approved from dashboard", ""}


def _latest_proposal(job: Job) -> Proposal | None:
    if not job.proposals:
        return None
    return sorted(job.proposals, key=lambda item: item.id)[-1]


def _material_edit(draft: str, edited: str) -> bool:
    left = " ".join((draft or "").split())
    right = " ".join((edited or "").split())
    if left == right:
        return False
    if abs(len(left) - len(right)) >= MIN_EDIT_CHARS:
        return True
    return left.lower() != right.lower() and max(len(left), len(right)) >= 20


def _existing_style_rules(db: Session) -> list[PreferenceRule]:
    return (
        db.query(PreferenceRule)
        .filter(PreferenceRule.category == STYLE_CATEGORY)
        .order_by(PreferenceRule.created_at.asc())
        .all()
    )


def _is_duplicate(candidate: str, existing: list[str]) -> bool:
    needle = " ".join(candidate.split()).lower()
    if not needle:
        return True
    for item in existing:
        if " ".join(item.split()).lower() == needle:
            return True
    if not existing:
        return False
    vectors = embed_texts(existing + [candidate])
    probe = vectors[-1]
    return any(cosine(probe, vector) >= DEDUPE_THRESHOLD for vector in vectors[:-1])


def _cap_learned(db: Session) -> None:
    learned = (
        db.query(PreferenceRule)
        .filter(
            PreferenceRule.category == STYLE_CATEGORY,
            PreferenceRule.enforcement_level == LEARNED_LEVEL,
            PreferenceRule.active.is_(True),
        )
        .order_by(PreferenceRule.created_at.asc())
        .all()
    )
    overflow = len(learned) - MAX_LEARNED_RULES
    if overflow <= 0:
        return
    for row in learned[:overflow]:
        row.active = False


def apply_learned_rules(db: Session, rules: list[str]) -> int:
    if not rules:
        return 0
    existing_rows = _existing_style_rules(db)
    existing_text = [row.rule for row in existing_rows if row.rule.strip()]
    added = 0
    for rule in rules:
        text = " ".join(rule.split())
        if not text or _is_duplicate(text, existing_text):
            continue
        db.add(
            PreferenceRule(
                category=STYLE_CATEGORY,
                rule=text,
                enforcement_level=LEARNED_LEVEL,
                active=True,
            )
        )
        existing_text.append(text)
        added += 1
    if added:
        db.flush()
        _cap_learned(db)
        db.flush()
    return added


def learn_style_from_job(job_id: int, outcome: str, notes: str = "") -> int:
    settings = get_settings()
    session = SessionLocal()
    try:
        job = (
            session.query(Job)
            .options(selectinload(Job.proposals))
            .filter(Job.id == job_id)
            .one_or_none()
        )
        if job is None:
            return 0
        proposal = _latest_proposal(job)
        draft = (proposal.draft_text or "") if proposal is not None else ""
        edited = (proposal.edited_text or "") if proposal is not None else ""
        reject_notes = ""
        draft_text = ""
        edited_text = ""
        if outcome == FeedbackOutcome.approved.value:
            if not _material_edit(draft, edited):
                return 0
            draft_text = draft
            edited_text = edited
        elif outcome == FeedbackOutcome.rejected.value:
            note = (notes or "").strip()
            if note.lower() in _GENERIC_REJECT:
                return 0
            reject_notes = note
            draft_text = edited or draft
        else:
            return 0
        rules = llm_extract_style_rules(
            settings,
            draft_text=draft_text,
            edited_text=edited_text,
            reject_notes=reject_notes,
        )
        added = apply_learned_rules(session, rules)
        session.commit()
        return added
    except Exception:
        session.rollback()
        logger.exception("style learning failed for job %s", job_id)
        return 0
    finally:
        session.close()


def schedule_style_learning(job_id: int, outcome: str, notes: str = "") -> None:
    if outcome not in {FeedbackOutcome.approved.value, FeedbackOutcome.rejected.value}:
        return

    def run() -> None:
        time.sleep(0.5)
        learn_style_from_job(job_id, outcome, notes)

    thread = threading.Thread(
        target=run,
        daemon=True,
        name=f"style-learn-{job_id}",
    )
    thread.start()
