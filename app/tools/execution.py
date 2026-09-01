from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db.models import Job, PortfolioItem, Proposal
from app.db.session import SessionLocal
from app.embeddings import cosine, embed_texts, query_similar
from app.engagement import classify_engagement
from app.events import add_event
from app.llm import llm_critique, llm_draft, llm_screening_answers
from app.milestones import (
    align_milestone_total,
    coerce_milestones,
    dump_milestones,
    heuristic_milestones,
    job_needs_milestones,
    load_milestones,
)
from app.models import (
    ApplyHighlight,
    ContextMatch,
    CritiqueResult,
    DraftResult,
    EmbeddingSource,
    FunnelStatus,
    GeneratePitchArgs,
    HighlightPicks,
    JobStatus,
    PitchTone,
    ProposalMilestone,
    ScreeningAnswer,
    SubmitProposalArgs,
    ToolSpec,
    TrackFunnelArgs,
    WorkKind,
)
from app.profile import load_overlay, load_profile
from app.proposal_settings import (
    DEFAULT_TARGET_WORDS,
    build_system_prompt,
    load_proposal_settings,
    select_examples,
    style_rules_for_prompt,
)
from app.proposal_writer import (
    dump_apply,
    dump_critique,
    dump_screening,
    extract_apply_questions,
    finalize_letter,
    load_apply,
    load_screening,
)
from app.tools import register_tool
from app.tools.discovery import settings_block_reasons_for_job
from app.upwork.mcp_client import UpworkMcpClient, already_applied, format_mcp_error

logger = logging.getLogger(__name__)

_DRAFTABLE_STATUSES = {JobStatus.pending_review.value, JobStatus.submit_failed.value}


class PitchSkipped(ValueError):
    pass


def _session(db: Session | None) -> tuple[Session, bool]:
    if db is not None:
        return db, False
    return SessionLocal(), True


def _find_job(session: Session, job_id: str) -> Job | None:
    if job_id.isdigit():
        job = session.query(Job).options(selectinload(Job.proposals)).filter(Job.id == int(job_id)).one_or_none()
        if job is not None:
            return job
    return session.query(Job).options(selectinload(Job.proposals)).filter(Job.upwork_id == job_id).one_or_none()


def _latest_proposal(job: Job) -> Proposal | None:
    if not job.proposals:
        return None
    return sorted(job.proposals, key=lambda item: item.id)[-1]


def _job_payload(job: Job) -> dict[str, str]:
    return {
        "id": job.upwork_id,
        "title": job.title,
        "description": job.description,
        "budget": job.budget or "",
        "price_label": job.price_label or job.budget or "",
        "client": job.client_info or "",
        "timezone": job.timezone or "",
        "job_type": job.job_type or "",
        "client_details": job.client_json or "",
    }


HIGHLIGHT_PICK = 4
_CONTRACT_STATUSES = {"closed", "active", "paused"}


def _row_highlight_kind(row: PortfolioItem) -> str | None:
    status = (row.outcomes_achieved or "").strip().lower()
    blob = (row.description or "").lower()
    if row.kind == WorkKind.employment.value or status == "employment":
        return "profile_history"
    if status in _CONTRACT_STATUSES or "client:" in blob:
        return "upwork_job"
    if row.kind == WorkKind.job_history.value:
        return "profile_history"
    return None


def local_job_highlights(session: Session) -> list[ApplyHighlight]:
    rows = (
        session.query(PortfolioItem)
        .filter(
            PortfolioItem.origin == "upwork",
            PortfolioItem.kind.in_([WorkKind.job_history.value, WorkKind.employment.value]),
        )
        .all()
    )
    items: list[ApplyHighlight] = []
    for row in rows:
        kind = _row_highlight_kind(row)
        if not kind:
            continue
        items.append(
            ApplyHighlight(
                kind=kind,
                id=str(row.id),
                title=row.project_title,
                detail=f"{row.description or ''} {row.outcomes_achieved or ''}".strip(),
            )
        )
    return items


def _kind_boost(kind: str) -> float:
    if kind == "upwork_job":
        return 0.08
    if kind == "portfolio":
        return 0.04
    if kind == "profile_history":
        return 0.01
    return 0.0


def _add_pick(picked: list[ApplyHighlight], seen: set[str], item: ApplyHighlight, limit: int) -> None:
    if item.id in seen or len(picked) >= limit:
        return
    picked.append(item)
    seen.add(item.id)


def _fill_highlights(
    picked: list[ApplyHighlight],
    seen: set[str],
    ordered: list[ApplyHighlight],
    highlights: list[ApplyHighlight],
    limit: int,
) -> None:
    for item in ordered:
        if item.kind == "upwork_job":
            _add_pick(picked, seen, item, limit)
            break
    else:
        for item in highlights:
            if item.kind == "upwork_job":
                _add_pick(picked, seen, item, limit)
                break
    for item in ordered:
        if item.kind in {"portfolio", "certificate"}:
            _add_pick(picked, seen, item, limit)
    for item in ordered:
        if item.kind == "profile_history":
            continue
        _add_pick(picked, seen, item, limit)


def _empty_picks() -> HighlightPicks:
    return {
        "portfolio_project_ids": [],
        "certificate_ids": [],
        "job_history_ids": [],
        "profile_history_ids": [],
    }


def _picks_from(picked: list[ApplyHighlight]) -> HighlightPicks:
    return {
        "portfolio_project_ids": [item.id for item in picked if item.kind == "portfolio"],
        "certificate_ids": [item.id for item in picked if item.kind == "certificate"],
        "job_history_ids": [item.id for item in picked if item.kind == "upwork_job"],
        "profile_history_ids": [],
    }


def highlight_pick_count(picks: HighlightPicks) -> int:
    return (
        len(picks["portfolio_project_ids"])
        + len(picks["certificate_ids"])
        + len(picks["job_history_ids"])
        + len(picks["profile_history_ids"])
    )


def cap_highlight_picks(
    portfolio_project_ids: list[str] | None = None,
    certificate_ids: list[str] | None = None,
    job_history_ids: list[str] | None = None,
    profile_history_ids: list[str] | None = None,
    limit: int = HIGHLIGHT_PICK,
) -> HighlightPicks:
    picked: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, ids in (
        ("upwork_job", job_history_ids or []),
        ("portfolio", portfolio_project_ids or []),
        ("certificate", certificate_ids or []),
    ):
        for hid in ids:
            key = str(hid)
            if not key or key in seen:
                continue
            if len(picked) >= limit:
                break
            picked.append((kind, key))
            seen.add(key)
        if len(picked) >= limit:
            break
    return {
        "portfolio_project_ids": [item_id for kind, item_id in picked if kind == "portfolio"],
        "certificate_ids": [item_id for kind, item_id in picked if kind == "certificate"],
        "job_history_ids": [item_id for kind, item_id in picked if kind == "upwork_job"],
        "profile_history_ids": [],
    }


_PROOF_KINDS = {
    WorkKind.job_history.value,
    WorkKind.project.value,
    WorkKind.employment.value,
}


def _is_employment(item: PortfolioItem) -> bool:
    status = (item.outcomes_achieved or "").strip().lower()
    return item.kind == WorkKind.employment.value or status == "employment"


def _proof_origin_label(item: PortfolioItem) -> str:
    if _is_employment(item):
        return "profile employment history"
    if item.kind == WorkKind.job_history.value:
        return "completed Upwork contract"
    return "portfolio project"


def _pick_proof(session: Session, matches: list[ContextMatch]) -> tuple[str, PortfolioItem | None]:
    ranked: list[tuple[ContextMatch, PortfolioItem]] = []
    for match in matches:
        if match.source_type != EmbeddingSource.portfolio.value:
            continue
        item = session.query(PortfolioItem).filter(PortfolioItem.id == match.source_id).one_or_none()
        if item is None or item.kind not in _PROOF_KINDS:
            continue
        ranked.append((match, item))
    if not ranked:
        return "", None
    preferred = [
        pair for pair in ranked if pair[1].kind == WorkKind.job_history.value and not _is_employment(pair[1])
    ]
    chosen, item = (preferred or ranked)[0]
    label = chosen.title or item.project_title or "relevant work"
    origin = _proof_origin_label(item)
    return f"[{origin}] {label}: {chosen.text[:1200]}", item


def rank_highlights(
    job_text: str,
    highlights: list[ApplyHighlight],
    proof: PortfolioItem | None = None,
    limit: int = HIGHLIGHT_PICK,
    settings: Settings | None = None,
) -> HighlightPicks:
    if not highlights:
        return _empty_picks()
    picked: list[ApplyHighlight] = []
    seen: set[str] = set()
    if proof and proof.external_id:
        for item in highlights:
            if item.kind == "profile_history":
                continue
            if item.id == proof.external_id or item.id == str(proof.id):
                picked.append(item)
                seen.add(item.id)
                break
    if proof and proof.project_title:
        needle = proof.project_title.lower()
        for item in highlights:
            if item.id in seen or item.kind == "profile_history":
                continue
            if needle in item.title.lower() or item.title.lower() in needle:
                picked.append(item)
                seen.add(item.id)
                break
    remaining = [item for item in highlights if item.id not in seen]
    scored: list[tuple[float, ApplyHighlight]] = []
    if remaining and len(picked) < limit:
        try:
            texts = [job_text] + [f"{item.kind} {item.title} {item.detail}".strip() for item in remaining]
            vectors = embed_texts(texts, settings)
            job_vec = vectors[0]
            scored = [
                (cosine(job_vec, vectors[index + 1]) + _kind_boost(item.kind), item)
                for index, item in enumerate(remaining)
            ]
            scored.sort(key=lambda row: row[0], reverse=True)
            _fill_highlights(picked, seen, [item for _, item in scored], highlights, limit)
        except Exception:
            logger.exception("highlight ranking failed; using title overlap")
            tokens = {tok for tok in re.findall(r"[a-z0-9]+", job_text.lower()) if len(tok) > 2}

            def overlap(item: ApplyHighlight) -> int:
                title_tokens = {tok for tok in re.findall(r"[a-z0-9]+", item.title.lower()) if len(tok) > 2}
                return len(tokens & title_tokens)

            remaining.sort(
                key=lambda item: (
                    item.kind != "upwork_job",
                    item.kind != "portfolio",
                    item.kind != "certificate",
                    -overlap(item),
                )
            )
            _fill_highlights(picked, seen, remaining, highlights, limit)
    else:
        _fill_highlights(picked, seen, remaining, highlights, limit)
    if not picked:
        picked = [item for item in highlights if item.kind != "profile_history"][:limit]
    return _picks_from([item for item in picked if item.kind != "profile_history"][:limit])


async def generate_tailored_pitch(
    job_id: str,
    tone: str = "consultative",
    focus_points: list[str] | None = None,
    db: Session | None = None,
    settings: Settings | None = None,
) -> DraftResult:
    try:
        tone_value = PitchTone(tone)
    except ValueError:
        tone_value = PitchTone.consultative
    args = GeneratePitchArgs(
        job_id=job_id,
        tone=tone_value,
        focus_points=focus_points or [],
    )
    settings = settings or get_settings()
    session, own = _session(db)
    try:
        job = _find_job(session, args.job_id)
        if job is None:
            raise ValueError(f"Unknown job {args.job_id}")
        if job.applied_on_upwork:
            raise PitchSkipped("Already applied on Upwork")
        if job.status not in _DRAFTABLE_STATUSES:
            raise PitchSkipped(f"Job status {job.status} cannot be drafted")
        if not (job.description or "").strip():
            raise ValueError("Job description is empty; cannot draft a proposal")
        block_reasons = settings_block_reasons_for_job(job, session, settings)
        if block_reasons:
            job.status = JobStatus.skipped.value
            job.expires_at = None
            note = "skipped by settings: " + "; ".join(block_reasons)
            job.score_reason = note + (f"; {job.score_reason}" if job.score_reason else "")
            add_event(session, "skipped", note, job.id)
            if own:
                session.commit()
            raise PitchSkipped(note)
        profile = load_profile(settings)
        writer = load_proposal_settings(session)
        try:
            args = GeneratePitchArgs(
                job_id=job_id,
                tone=PitchTone(writer.tone),
                focus_points=focus_points or [],
            )
        except ValueError:
            args = GeneratePitchArgs(job_id=job_id, tone=PitchTone.consultative, focus_points=focus_points or [])
        try:
            details = json.loads(job.client_json or "{}")
        except json.JSONDecodeError:
            details = {}
        attachment_text = str(details.get("attachment_text") or "") if isinstance(details, dict) else ""
        blob = f"{job.title}\n{job.description}\n{attachment_text}"
        matches = query_similar(session, blob, top_k=8, source_type=EmbeddingSource.portfolio.value)
        proof_text, proof_item = _pick_proof(session, matches)
        payload = _job_payload(job)
        bid = bid_amount_for_job(job, profile.hourly_rate)
        need_plan = job_needs_milestones(job, bid)
        style_examples = select_examples(session, blob, writer.example_count)
        engagement = classify_engagement(job.title or "", job.description or "", job.job_type or "")
        system_prompt = build_system_prompt(writer, profile, style_rules_for_prompt(session), engagement=engagement)
        stage_payload = [item.model_dump() for item in writer.milestone_stages]

        def run_draft(focus: list[str]) -> DraftResult:
            result = llm_draft(
                payload,
                profile,
                settings,
                proof=proof_text,
                tone=args.tone.value,
                focus_points=focus,
                milestones_budget=bid if need_plan else None,
                system_prompt=system_prompt,
                style_examples=style_examples,
                milestone_min=writer.milestone_min,
                milestone_max=writer.milestone_max,
                apply_questions_instructions=writer.apply_questions_instructions,
                screening_instructions=writer.screening_instructions,
                opening_hook=writer.opening_hook,
                enforce_hook=writer.enforce_opening_hook,
            )
            if need_plan:
                planned = coerce_milestones([item.model_dump() for item in result.milestones], bid) or heuristic_milestones(
                    job, bid, stage_payload
                )
                result.milestones = planned
            result.cover_letter = finalize_letter(
                result.cover_letter,
                hook=writer.opening_hook,
                enforce=writer.enforce_opening_hook,
            )
            return result

        drafted = run_draft(list(args.focus_points))
        critique = CritiqueResult(passed=True, issues=[], rounds=0)
        rounds = max(0, writer.critique_rounds)
        target_words = writer.target_words or DEFAULT_TARGET_WORDS
        apply_items = extract_apply_questions(job.description or "")
        for round_index in range(rounds):
            try:
                critique = llm_critique(
                    drafted.cover_letter,
                    payload,
                    profile,
                    settings,
                    target_words=target_words,
                    apply_questions=apply_items,
                )
            except Exception:
                logger.exception("critique failed for job %s", job.id)
                critique = CritiqueResult(passed=True, issues=[], rounds=round_index + 1)
                break
            critique.rounds = round_index + 1
            if critique.passed:
                break
            focus = list(args.focus_points) + critique.issues
            drafted = run_draft(focus)
        drafted.critique = critique
        drafted.matched_context = [proof_text] if proof_text else []
        portfolio_ids: list[str] = []
        certificate_ids: list[str] = []
        job_ids: list[str] = []
        profile_ids: list[str] = []
        screening: list[ScreeningAnswer] = list(drafted.screening_answers)
        client = UpworkMcpClient()
        try:
            highlights = await client.list_highlights()
        except Exception:
            logger.exception("list_highlights failed for job %s", job.id)
            highlights = []
        highlights.extend(local_job_highlights(session))
        if highlights:
            picks = rank_highlights(
                blob,
                highlights,
                proof=proof_item,
                settings=settings,
            )
            portfolio_ids = picks["portfolio_project_ids"]
            certificate_ids = picks["certificate_ids"]
            job_ids = picks["job_history_ids"]
            profile_ids = picks["profile_history_ids"]
        try:
            preview = await client.preview_proposal(job.upwork_id, drafted.cover_letter, bid)
        except Exception:
            logger.exception("proposal preview failed for job %s", job.id)
            preview = None
        questions = preview.screening_questions if preview is not None else []
        if questions:
                screening = llm_screening_answers(
                    payload,
                    profile,
                    settings,
                    drafted.cover_letter,
                    questions,
                    screening_instructions=writer.screening_instructions,
                    proof=proof_text,
                )
        drafted.screening_answers = screening
        drafted.portfolio_project_ids = portfolio_ids
        drafted.certificate_ids = certificate_ids
        drafted.job_history_ids = job_ids
        drafted.profile_history_ids = profile_ids
        proposal = _latest_proposal(job)
        if proposal is None:
            proposal = Proposal(job_id=job.id, draft_text=drafted.cover_letter, edited_text=drafted.cover_letter)
            session.add(proposal)
        else:
            proposal.draft_text = drafted.cover_letter
            proposal.edited_text = drafted.cover_letter
        proposal.milestones_json = dump_milestones(drafted.milestones)
        proposal.screening_json = dump_screening(drafted.screening_answers)
        proposal.apply_json = dump_apply(
            {
                "portfolio_project_ids": portfolio_ids,
                "certificate_ids": certificate_ids,
                "job_history_ids": job_ids,
                "profile_history_ids": profile_ids,
                "proof": proof_text[:300],
            }
        )
        proposal.critique_json = dump_critique(critique)
        job.matched_context = json.dumps(drafted.matched_context)
        session.flush()
        add_event(
            session,
            "drafted",
            "Cover letter drafted" + (f" with {len(drafted.milestones)} milestones" if drafted.milestones else ""),
            job.id,
        )
        if own:
            session.commit()
        return drafted
    except PitchSkipped:
        raise
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


_FUNNEL_TO_JOB = {
    FunnelStatus.new: JobStatus.pending_review,
    FunnelStatus.skipped: JobStatus.skipped,
    FunnelStatus.pitch_drafted: JobStatus.pending_review,
    FunnelStatus.pending_review: JobStatus.pending_review,
    FunnelStatus.submitted: JobStatus.submitted,
}


def bid_amount_for_job(job: Job, hourly_rate: int | None) -> float:
    if (job.job_type or "").lower() in {"fixed", "fixed_price"}:
        try:
            data = json.loads(job.client_json or "{}")
        except json.JSONDecodeError:
            data = {}
        budget = data.get("budget")
        if isinstance(budget, (int, float)) and budget > 0:
            return float(budget)
        label = str(job.price_label or job.budget or "")
        digits = "".join(ch if ch.isdigit() or ch == "." else " " for ch in label)
        parts = [part for part in digits.split() if part]
        if parts:
            try:
                amount = float(parts[0])
                if amount > 0:
                    return amount
            except ValueError:
                pass
    if hourly_rate:
        return float(hourly_rate)
    return 50.0


async def track_application_funnel(
    job_id: str,
    status: str,
    db: Session | None = None,
    settings: Settings | None = None,
) -> dict[str, str]:
    args = TrackFunnelArgs(job_id=job_id, status=FunnelStatus(status))
    settings = settings or get_settings()
    session, own = _session(db)
    try:
        job = _find_job(session, args.job_id)
        if job is None:
            raise ValueError(f"Unknown job {args.job_id}")
        mapped = _FUNNEL_TO_JOB[args.status]
        job.status = mapped.value
        if mapped == JobStatus.pending_review:
            job.expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_ttl_hours)
        add_event(session, "funnel", args.status.value, job.id)
        if own:
            session.commit()
        return {"job_id": job.upwork_id, "status": job.status, "funnel": args.status.value}
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


async def submit_proposal(
    job_id: str,
    cover_letter: str,
    db: Session | None = None,
    boost_connects: int = 0,
    charged_amount: float | None = None,
    milestones: list[ProposalMilestone] | None = None,
    screening_answers: list[ScreeningAnswer] | None = None,
    portfolio_project_ids: list[str] | None = None,
    certificate_ids: list[str] | None = None,
    attachment_uids: list[str] | None = None,
) -> dict[str, str]:
    args = SubmitProposalArgs(
        job_id=job_id,
        cover_letter=cover_letter,
        boost_connects=max(0, boost_connects),
        charged_amount=charged_amount,
        milestones=milestones or [],
        screening_answers=screening_answers or [],
        portfolio_project_ids=portfolio_project_ids or [],
        certificate_ids=certificate_ids or [],
        attachment_uids=attachment_uids or [],
    )
    session, own = _session(db)
    try:
        job = _find_job(session, args.job_id)
        if job is None:
            raise ValueError(f"Unknown job {args.job_id}")
        letter = args.cover_letter.strip()
        if not letter:
            raise ValueError("Cover letter is empty")
        settings = get_settings()
        overlay = load_overlay(settings)
        bid = args.charged_amount if args.charged_amount is not None else bid_amount_for_job(job, overlay.hourly_rate)
        client = UpworkMcpClient()
        proposal = _latest_proposal(job)
        planned = args.milestones or load_milestones(proposal)
        writer = load_proposal_settings(session)
        if not planned and job_needs_milestones(job, bid):
            planned = heuristic_milestones(job, bid, [item.model_dump() for item in writer.milestone_stages])
        if planned:
            planned = align_milestone_total(planned, bid)
        letter = finalize_letter(letter, hook=writer.opening_hook, enforce=writer.enforce_opening_hook)
        answers = args.screening_answers or load_screening(proposal)
        apply_payload = load_apply(proposal)
        portfolio_ids = args.portfolio_project_ids or [str(item) for item in apply_payload.get("portfolio_project_ids") or [] if item]
        cert_ids = args.certificate_ids or [str(item) for item in apply_payload.get("certificate_ids") or [] if item]
        job_ids = [str(item) for item in apply_payload.get("job_history_ids") or [] if item]
        profile_ids = [str(item) for item in apply_payload.get("profile_history_ids") or [] if item]
        capped = cap_highlight_picks(
            portfolio_project_ids=portfolio_ids,
            certificate_ids=cert_ids,
            job_history_ids=job_ids,
            profile_history_ids=profile_ids,
        )
        portfolio_ids = capped["portfolio_project_ids"]
        cert_ids = capped["certificate_ids"]
        try:
            apply_cost: int | None = None
            try:
                details = json.loads(job.client_json or "{}")
                cost = details.get("connects_cost")
                if isinstance(cost, (int, float)):
                    apply_cost = int(cost)
            except json.JSONDecodeError:
                apply_cost = None
            if apply_cost is not None:
                try:
                    balance = await client.get_connects_balance()
                except Exception:
                    balance = None
                total = apply_cost + args.boost_connects
                if balance is not None and total > balance:
                    raise RuntimeError(f"Not enough Connects: need {total}, have {balance}")
            result = await client.submit_proposal(
                job.upwork_id,
                letter,
                bid,
                args.boost_connects,
                planned,
                answers=answers,
                portfolio_project_ids=portfolio_ids,
                certificate_ids=cert_ids,
                attachments=args.attachment_uids,
            )
            job.status = JobStatus.submitted.value
            job.applied_on_upwork = True
            if proposal is not None:
                proposal.submitted_text = letter
                proposal.submit_error = None
                proposal.milestones_json = dump_milestones(planned)
                proposal.screening_json = dump_screening(answers)
                proposal.apply_json = dump_apply(
                    {
                        **apply_payload,
                        "portfolio_project_ids": portfolio_ids,
                        "certificate_ids": cert_ids,
                    }
                )
            note = "Already on Upwork" if already_applied(result) or result == "already_applied" else f"{result[:1500]} bid={bid:g}"
            add_event(session, "submitted", note, job.id)
            add_embedding(session, EmbeddingSource.job, job.id, f"{job.title}\n{job.description}")
            if proposal is not None:
                add_embedding(session, EmbeddingSource.proposal, proposal.id, letter)
            if own:
                session.commit()
            return {"status": job.status, "result": result}
        except Exception as exc:
            detail = format_mcp_error(exc)
            if already_applied(detail):
                job.status = JobStatus.submitted.value
                job.applied_on_upwork = True
                add_event(session, "submitted", "Already applied on Upwork", job.id)
                if own:
                    session.commit()
                return {"status": job.status, "result": detail}
            job.status = JobStatus.submit_failed.value
            logger.exception("submit_proposal failed for job %s", job.id)
            if proposal is not None:
                proposal.submit_error = detail
            add_event(session, "submit_failed", detail, job.id)
            if own:
                session.commit()
            return {"status": job.status, "result": detail}
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


async def redraft_open_jobs(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    session = SessionLocal()
    try:
        rows = (
            session.query(Job)
            .filter(
                Job.status.in_([JobStatus.pending_review.value, JobStatus.submit_failed.value]),
                Job.applied_on_upwork.is_(False),
            )
            .all()
        )
        job_ids = [row.id for row in rows]
    finally:
        session.close()
    count = 0
    for job_id in job_ids:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).one_or_none()
            if job is None:
                continue
            if not (job.description or "").strip():
                add_event(db, "redraft_skipped", "Empty description", job.id)
                db.commit()
                continue
            print(f"redrafting job {job.id}", flush=True)
            await generate_tailored_pitch(str(job.id), db=db, settings=settings)
            add_event(db, "redrafted", "Cover letter rewritten with the strategy writer", job.id)
            db.commit()
            count += 1
            logger.info("redrafted job %s", job.id)
            print(f"redrafted job {job.id}", flush=True)
        except PitchSkipped as exc:
            db.commit()
            logger.info("redraft skipped job %s: %s", job_id, exc)
        except Exception:
            db.rollback()
            logger.exception("redraft failed for job %s", job_id)
        finally:
            db.close()
    return count


register_tool(
    ToolSpec(
        name="generate_tailored_pitch",
        description="Draft a conversion-oriented cover letter using profile, tone, and matching portfolio context.",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "tone": {
                    "type": "string",
                    "enum": ["assertive", "technical_peer", "consultative"],
                },
                "focus_points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["job_id"],
        },
    ),
    generate_tailored_pitch,
)

register_tool(
    ToolSpec(
        name="track_application_funnel",
        description="Update the internal tracker status for a job.",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["new", "skipped", "pitch_drafted", "pending_review", "submitted"],
                },
            },
            "required": ["job_id", "status"],
        },
    ),
    track_application_funnel,
)

register_tool(
    ToolSpec(
        name="submit_proposal",
        description="Submit a cover letter to Upwork via MCP. Spends Connects.",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "cover_letter": {"type": "string"},
                "boost_connects": {"type": "integer"},
            },
            "required": ["job_id", "cover_letter"],
        },
    ),
    submit_proposal,
)
