from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.actions import approve_and_submit, cover_letter_for, latest_proposal, reject_job, save_edit
from app.auth import COOKIE_NAME, create_session_value, current_user, verify_password
from app.config import Settings, get_settings
from app.models import ApplyHighlight, ConnectsPanel, FreelancerProfile, InboxCounts, InboxSort, JobStatus, MilestoneStageConfig, PitchTone, ScreeningAnswer, SessionUser, WorkKind, WorkOrigin, WriterConfig
from app.db.models import ChatMessage, Event, FeedbackLog, Job, MessageRoom, PortfolioItem, PreferenceRule, ProposalExample, UpworkApplication, UpworkProfile, User
from app.llm import llm_suggest_reply
from app.db.session import SessionLocal, get_db
from app.engagement import classify_engagement
from app.events import add_event
from app.job_attachments import safe_filename
from app.job_display import application_card, job_card, sort_job_cards
from app.milestones import heuristic_milestones, job_needs_milestones, load_milestones, parse_milestone_form
from app.profile import load_overlay, load_profile, save_overlay
from app.proposal_settings import (
    apply_config_to_row,
    build_system_prompt,
    get_or_create_proposal_settings,
    load_proposal_settings,
    reset_proposal_settings,
    reset_role_letter_structure,
    style_rules_for_prompt,
)
from app.proposal_writer import dump_apply, finalize_letter, load_apply, load_screening, parse_screening_form
from app.runtime import get_or_create_runtime
from app.tools.discovery import apply_runtime_filters
from app.tools.execution import HIGHLIGHT_PICK, PitchSkipped, bid_amount_for_job, generate_tailored_pitch, local_job_highlights, rank_highlights
from app.tools.memory import learn_preference, log_interaction_feedback, save_agent_item, update_portfolio_matrix
from app.upwork.mcp_client import UpworkMcpClient, already_applied, connects_shortage, format_mcp_error, oauth_needs_login, public_job_url
from app.upwork.oauth import (
    OAuthCallbackPayload,
    WebOAuthFlow,
    clear_web_oauth_flow,
    get_web_oauth_flow,
    pop_last_oauth_error,
    set_last_oauth_error,
    start_web_oauth_flow,
)
from app.upwork.messages import (
    first_message_blocked,
    load_rooms,
    message_views,
    related_job_id,
    refresh_room,
    save_suggested_draft,
    sync_messages,
    thread_card,
)
from app.upwork.sync import mark_jobs_applied, sync_upwork_memory
from app.worker import run_poll_cycle

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")
logger = logging.getLogger(__name__)
_STYLE_PATH = Path(__file__).resolve().parent / "static" / "style.css"
_ASSET_V = str(int(_STYLE_PATH.stat().st_mtime)) if _STYLE_PATH.exists() else "1"


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _upwork_job_filter(value: str | None) -> str:
    return public_job_url(str(value) if value else "")


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["upwork_job"] = _upwork_job_filter


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200) -> Response:
    settings = get_settings()
    payload = dict(context or {})
    payload.pop("request", None)
    payload.setdefault("app_name", settings.app_name)
    payload.setdefault("app_tagline", settings.app_tagline)
    payload.setdefault("asset_v", _ASSET_V)
    return templates.TemplateResponse(request, name, payload, status_code=status_code)


def _inbox_auth_stale(events: Sequence[Event], oauth: str | None) -> bool:
    if oauth == "ok":
        return False
    for item in events:
        text = (item.message or "").lower()
        if item.kind == "upwork" and "connected" in text and "failed" not in text:
            return False
        if item.kind == "poll_error" and oauth_needs_login(item.message):
            return True
        if item.kind == "poll" and "session expired" in text:
            return True
    return False


INBOX_SORTS: tuple[tuple[InboxSort, str], ...] = (
    (InboxSort.recent, "Recent"),
    (InboxSort.score, "Score"),
    (InboxSort.oldest, "Oldest"),
    (InboxSort.score_low, "Score (low)"),
)


def _inbox_order(sort: InboxSort) -> tuple[object, ...]:
    if sort == InboxSort.oldest:
        return (Job.created_at.asc(),)
    if sort == InboxSort.score:
        return (Job.score.desc().nulls_last(), Job.created_at.desc())
    if sort == InboxSort.score_low:
        return (Job.score.asc().nulls_last(), Job.created_at.desc())
    return (Job.created_at.desc(),)


def _counts(db: Session) -> InboxCounts:
    rows = db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    tallies = {status: count for status, count in rows}
    pending = (
        db.query(func.count(Job.id))
        .filter(Job.status == JobStatus.pending_review.value, Job.applied_on_upwork.is_(False))
        .scalar()
    )
    return InboxCounts(
        pending_review=int(pending or 0),
        submitted=int(tallies.get(JobStatus.submitted.value, 0)),
        rejected=int(tallies.get(JobStatus.rejected.value, 0)),
        submit_failed=int(tallies.get(JobStatus.submit_failed.value, 0)),
        expired=int(tallies.get(JobStatus.expired.value, 0)),
        applied=int(db.query(UpworkApplication).count())
        or int(db.query(Job).filter(Job.applied_on_upwork.is_(True)).count()),
    )


def _user(request: Request) -> SessionUser:
    user = current_user(request)
    if user is None:
        raise RuntimeError("unauthenticated")
    return user


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None) -> Response:
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"error": error})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return render(
            request,
            "login.html",
            {"error": "Invalid username or password"},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        create_session_value(settings, user.username),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 14,
    )
    return response


@router.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/", response_class=HTMLResponse)
async def inbox(
    request: Request,
    status: str = Query(default=JobStatus.pending_review.value),
    sort: str = Query(default=InboxSort.recent.value),
    oauth: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Response:
    user = _user(request)
    allowed = {item.value for item in JobStatus} | {"all", "applied"}
    selected = status if status in allowed else JobStatus.pending_review.value
    selected_sort = InboxSort.recent
    try:
        selected_sort = InboxSort(sort)
    except ValueError:
        selected_sort = InboxSort.recent
    query = db.query(Job).options(selectinload(Job.proposals)).order_by(*_inbox_order(selected_sort))
    if selected == "applied":
        query = query.filter((Job.applied_on_upwork.is_(True)) | (Job.status == JobStatus.submitted.value))
    elif selected == JobStatus.pending_review.value:
        query = query.filter(Job.status == selected, Job.applied_on_upwork.is_(False))
    elif selected != "all":
        query = query.filter(Job.status == selected)
    jobs: Sequence[Job] = query.limit(200).all()
    application_rows = db.query(UpworkApplication).order_by(UpworkApplication.synced_at.desc()).all()
    applications = {row.posting_id: row.status for row in application_rows}
    cards = [job_card(job, applied_status=applications.get(job.upwork_id, "")) for job in jobs]
    if selected == "applied":
        seen = {job.upwork_id for job in jobs}
        cards.extend(application_card(row) for row in application_rows if row.posting_id not in seen)
    cards = sort_job_cards(cards, selected_sort)
    events = db.query(Event).order_by(Event.created_at.desc()).limit(12).all()
    mcp = UpworkMcpClient()
    connected = await mcp.is_authenticated()
    stale_auth = _inbox_auth_stale(events, oauth)
    mcp_status = {
        "connected": connected and not stale_auth,
        "tools": [],
        "error": "Upwork session expired. Connect again." if stale_auth else ("" if connected else "Not logged in"),
        "stale": stale_auth,
    }
    oauth_notice = {
        "ok": "Upwork connected. Polling can fetch jobs now.",
        "failed": "Upwork login failed. Try Connect Upwork again.",
        "timeout": "Upwork login timed out. Try Connect Upwork again.",
        "missing": "No Upwork login was in progress. Click Connect Upwork.",
        "pending": "Upwork login already started. Finish it in the other tab.",
    }.get(oauth or "", "")
    runtime = get_or_create_runtime(db)
    return render(
        request,
        "inbox.html",
        {
            "user": user,
            "jobs": cards,
            "status": selected,
            "sort": selected_sort.value,
            "sort_options": INBOX_SORTS,
            "counts": _counts(db),
            "events": events,
            "mcp": mcp_status,
            "runtime": runtime,
            "oauth_notice": oauth_notice,
            "oauth": oauth or "",
            "oauth_detail": pop_last_oauth_error() if oauth == "failed" else "",
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int, db: Session = Depends(get_db)) -> Response:
    user = _user(request)
    job = db.query(Job).options(selectinload(Job.proposals), selectinload(Job.events)).filter(Job.id == job_id).one_or_none()
    if job is None:
        return RedirectResponse("/", status_code=303)
    events = sorted(job.events, key=lambda item: item.created_at, reverse=True)
    breakdown: list[str] = []
    if job.score_breakdown:
        try:
            parsed = json.loads(job.score_breakdown)
            if isinstance(parsed, list):
                breakdown = [str(item) for item in parsed]
        except json.JSONDecodeError:
            breakdown = [job.score_breakdown]
    context: list[str] = []
    if job.matched_context:
        try:
            parsed_ctx = json.loads(job.matched_context)
            if isinstance(parsed_ctx, list):
                context = [str(item) for item in parsed_ctx]
        except json.JSONDecodeError:
            context = [job.matched_context]
    outcomes = (
        db.query(FeedbackLog)
        .filter(FeedbackLog.job_id == job.id)
        .order_by(FeedbackLog.created_at.desc())
        .all()
    )
    can_act = job.status in {JobStatus.pending_review.value, JobStatus.submit_failed.value} and not job.applied_on_upwork
    applied_ids = {row.posting_id for row in db.query(UpworkApplication).all()}
    if can_act and mark_jobs_applied(db, applied_ids, "Matched an Upwork proposal"):
        db.refresh(job)
        can_act = job.status in {JobStatus.pending_review.value, JobStatus.submit_failed.value} and not job.applied_on_upwork
    writer = load_proposal_settings(db)
    letter = cover_letter_for(job, db)
    overlay = load_overlay(get_settings())
    bid = bid_amount_for_job(job, overlay.hourly_rate)
    panel = ConnectsPanel(charged_amount=bid)
    highlights: list[ApplyHighlight] = []
    if can_act:
        mcp = UpworkMcpClient()
        try:
            if job.applied_on_upwork:
                panel = ConnectsPanel(
                    available=await mcp.get_connects_balance(),
                    charged_amount=bid,
                    error="Already applied on Upwork",
                )
            elif letter.strip():
                panel = await mcp.preview_proposal(job.upwork_id, letter, bid)
            else:
                panel = ConnectsPanel(
                    available=await mcp.get_connects_balance(),
                    charged_amount=bid,
                    error="Cover letter is empty",
                )
        except Exception as exc:
            logger.exception("proposal preview failed")
            detail = format_mcp_error(exc)
            try:
                available = await mcp.get_connects_balance()
            except Exception:
                available = None
            panel = ConnectsPanel(available=available, charged_amount=bid, error=detail)
        if already_applied(panel.error):
            mark_jobs_applied(db, {job.upwork_id}, "Upwork reports an existing proposal")
            db.refresh(job)
            can_act = False
            panel.can_apply = False
            panel.error = "Already applied on Upwork"
        try:
            highlights = await mcp.list_highlights()
        except Exception:
            logger.exception("list_highlights failed")
        highlights.extend(local_job_highlights(db))
        listing_cost: int | None = None
        try:
            details = json.loads(job.client_json or "{}")
            cost = details.get("connects_cost")
            if isinstance(cost, (int, float)):
                listing_cost = int(cost)
        except json.JSONDecodeError:
            listing_cost = None
        panel.listing_cost = listing_cost
        if panel.apply_cost is None and listing_cost is not None and not connects_shortage(panel.error):
            panel.apply_cost = listing_cost
        if panel.available is not None and panel.apply_cost is not None:
            panel.remaining_after_apply = panel.available - panel.apply_cost
        if panel.remaining_after_apply is not None and panel.remaining_after_apply < 0:
            panel.can_apply = False
    allowed = None if panel.error else panel.milestones_allowed
    proposal = latest_proposal(job)
    milestones = load_milestones(proposal)
    if can_act and not milestones and job_needs_milestones(job, bid, allowed):
        milestones = heuristic_milestones(job, bid, [item.model_dump() for item in writer.milestone_stages])
    stored_answers = load_screening(proposal)
    questions = panel.screening_questions or [item.question for item in stored_answers]
    by_question = {item.question: item.answer for item in stored_answers}
    screening = [ScreeningAnswer(question=item, answer=by_question.get(item, "")) for item in questions]
    apply_payload = load_apply(proposal)
    selected_projects = {str(item) for item in apply_payload.get("portfolio_project_ids") or [] if item}
    selected_certs = {str(item) for item in apply_payload.get("certificate_ids") or [] if item}
    selected_jobs = {str(item) for item in apply_payload.get("job_history_ids") or [] if item}
    jobs_saved = "job_history_ids" in apply_payload
    had_profile = bool(apply_payload.get("profile_history_ids"))
    selected_total = len(selected_projects) + len(selected_certs) + len(selected_jobs)
    if can_act and highlights:
        empty = not selected_projects and not selected_certs and not selected_jobs
        if empty or not jobs_saved or selected_total > HIGHLIGHT_PICK or had_profile:
            picks = rank_highlights(f"{job.title}\n{job.description}", highlights)
            selected_projects = set(picks["portfolio_project_ids"])
            selected_certs = set(picks["certificate_ids"])
            selected_jobs = set(picks["job_history_ids"])
            apply_payload.update(picks)
            apply_payload["profile_history_ids"] = []
            if proposal is not None:
                proposal.apply_json = dump_apply(apply_payload)
    formatted = finalize_letter(letter, hook=writer.opening_hook, enforce=writer.enforce_opening_hook)
    if can_act and proposal is not None and formatted != letter:
        proposal.edited_text = formatted
        letter = formatted
    for item in highlights:
        if item.kind == "portfolio":
            item.selected = item.id in selected_projects
        elif item.kind == "certificate":
            item.selected = item.id in selected_certs
        elif item.kind == "upwork_job":
            item.selected = item.id in selected_jobs
        else:
            item.selected = False
    if not highlights:
        for hid in selected_projects:
            highlights.append(ApplyHighlight(kind="portfolio", id=hid, title=hid, selected=True))
        for hid in selected_certs:
            highlights.append(ApplyHighlight(kind="certificate", id=hid, title=hid, selected=True))
        for hid in selected_jobs:
            highlights.append(ApplyHighlight(kind="upwork_job", id=hid, title=hid, selected=True))
    kind_order = {"upwork_job": 0, "portfolio": 1, "certificate": 2, "profile_history": 3}
    highlights.sort(key=lambda item: (not item.selected, kind_order.get(item.kind, 9), item.title.lower()))
    submit_error = (proposal.submit_error if proposal is not None else None) or ""
    if not submit_error:
        for event in events:
            if event.kind == "submit_failed" and event.message:
                submit_error = event.message
                break
    oauth_stale = oauth_needs_login(submit_error)
    return render(
        request,
        "job.html",
        {
            "user": user,
            "job": job,
            "cover_letter": letter,
            "events": events,
            "breakdown": breakdown,
            "matched_context": context,
            "outcomes": outcomes,
            "can_act": can_act,
            "card": job_card(job),
            "connects": panel,
            "milestones": milestones,
            "show_milestones": bool(milestones) or job_needs_milestones(job, bid, allowed),
            "screening": screening,
            "highlights": highlights,
            "engagement": classify_engagement(job.title or "", job.description or "", job.job_type or ""),
            "submit_error": submit_error,
            "oauth_stale": oauth_stale,
        },
    )


@router.get("/jobs/{job_id}/files/{filename}")
def job_attachment_file(
    request: Request,
    job_id: int,
    filename: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    _user(request)
    job = db.query(Job).filter(Job.id == job_id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404)
    safe = Path(filename).name
    root = (settings.data_dir / "job_files" / safe_filename(job.upwork_id)).resolve()
    path = (root / safe).resolve()
    if not path.is_file():
        path = (root / safe_filename(safe)).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=safe)


@router.post("/jobs/{job_id}/edit")
def edit_job(
    job_id: int,
    cover_letter: str = Form(...),
    milestones_present: str = Form(""),
    ms_title: list[str] = Form(default=[]),
    ms_description: list[str] = Form(default=[]),
    ms_amount: list[str] = Form(default=[]),
    sq_question: list[str] = Form(default=[]),
    sq_answer: list[str] = Form(default=[]),
    portfolio_project_ids: list[str] = Form(default=[]),
    certificate_ids: list[str] = Form(default=[]),
    job_history_ids: list[str] = Form(default=[]),
    profile_history_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
) -> Response:
    job = db.query(Job).options(selectinload(Job.proposals)).filter(Job.id == job_id).one_or_none()
    if job is None:
        return RedirectResponse("/", status_code=303)
    planned = parse_milestone_form(ms_description, ms_amount, ms_title) if milestones_present.strip() else None
    screening = parse_screening_form(sq_question, sq_answer)
    save_edit(
        db,
        job,
        cover_letter,
        planned,
        screening_answers=screening,
        portfolio_project_ids=portfolio_project_ids,
        certificate_ids=certificate_ids,
        job_history_ids=job_history_ids,
        profile_history_ids=profile_history_ids,
    )
    return RedirectResponse(f"/jobs/{job.id}?saved=1", status_code=303)


@router.post("/jobs/{job_id}/regenerate")
async def regenerate_job(
    request: Request,
    job_id: int,
    comments: str = Form(""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    _user(request)
    job = db.query(Job).options(selectinload(Job.proposals)).filter(Job.id == job_id).one_or_none()
    if job is None:
        return RedirectResponse("/", status_code=303)
    if job.applied_on_upwork or job.status not in {JobStatus.pending_review.value, JobStatus.submit_failed.value}:
        return RedirectResponse(f"/jobs/{job.id}?error=blocked", status_code=303)
    points = [line.strip(" -•\t") for line in comments.splitlines() if line.strip()]
    try:
        await generate_tailored_pitch(str(job.id), focus_points=points, db=db, settings=settings)
    except PitchSkipped:
        return RedirectResponse(f"/jobs/{job.id}?skipped=1", status_code=303)
    except Exception:
        logger.exception("regenerate failed for job %s", job.id)
        return RedirectResponse(f"/jobs/{job.id}?error=failed", status_code=303)
    note = "Proposal regenerated"
    if comments.strip():
        note += f": {comments.strip()[:240]}"
    add_event(db, "regenerated", note, job.id)
    return RedirectResponse(f"/jobs/{job.id}?regenerated=1", status_code=303)


@router.post("/jobs/{job_id}/approve")
async def approve_job(
    job_id: int,
    cover_letter: str = Form(""),
    boost_connects: str = Form(""),
    milestones_present: str = Form(""),
    ms_title: list[str] = Form(default=[]),
    ms_description: list[str] = Form(default=[]),
    ms_amount: list[str] = Form(default=[]),
    sq_question: list[str] = Form(default=[]),
    sq_answer: list[str] = Form(default=[]),
    portfolio_project_ids: list[str] = Form(default=[]),
    certificate_ids: list[str] = Form(default=[]),
    job_history_ids: list[str] = Form(default=[]),
    profile_history_ids: list[str] = Form(default=[]),
    attach_choice: str = Form("skip"),
    proposal_files: list[UploadFile] | None = File(default=None),
    db: Session = Depends(get_db),
) -> Response:
    job = db.query(Job).options(selectinload(Job.proposals)).filter(Job.id == job_id).one_or_none()
    if job is None:
        return RedirectResponse("/", status_code=303)
    boost = 0
    if boost_connects.strip():
        try:
            boost = max(0, int(float(boost_connects)))
        except ValueError:
            boost = 0
    planned = parse_milestone_form(ms_description, ms_amount, ms_title) if milestones_present.strip() else None
    screening = parse_screening_form(sq_question, sq_answer)
    attachment_uids: list[str] = []
    try:
        if attach_choice.strip() == "files":
            uploads = [item for item in (proposal_files or []) if item.filename]
            if not uploads:
                raise ValueError("Choose files or select Skip attachments")
            packed: list[tuple[str, bytes, str]] = []
            for item in uploads:
                packed.append(
                    (
                        item.filename or "attachment",
                        await item.read(),
                        item.content_type or "application/octet-stream",
                    )
                )
            attachment_uids = await UpworkMcpClient().upload_proposal_files(packed)
        await approve_and_submit(
            db,
            job,
            cover_letter=cover_letter,
            boost_connects=boost,
            milestones=planned,
            screening_answers=screening,
            portfolio_project_ids=portfolio_project_ids,
            certificate_ids=certificate_ids,
            job_history_ids=job_history_ids,
            profile_history_ids=profile_history_ids,
            attachment_uids=attachment_uids,
        )
    except (ValueError, RuntimeError, Exception) as exc:
        add_event(db, "submit_failed", format_mcp_error(exc), job.id)
        logger.exception("approve/submit failed for job %s", job.id)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/jobs/{job_id}/reject")
async def reject(
    job_id: int,
    reason: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    job = db.query(Job).options(selectinload(Job.proposals)).filter(Job.id == job_id).one_or_none()
    if job is None:
        return RedirectResponse("/", status_code=303)
    await reject_job(db, job, reason)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/poll")
async def poll_now() -> Response:
    await run_poll_cycle()
    return RedirectResponse("/", status_code=303)


async def _complete_web_login(flow: WebOAuthFlow) -> None:
    try:
        tools = await UpworkMcpClient().login(mode="web", web_flow=flow)
        db = SessionLocal()
        try:
            add_event(db, "upwork", f"MCP connected ({len(tools)} tools)")
            db.commit()
        finally:
            db.close()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        flow.exception = exc
        logger.exception("Upwork OAuth failed")
        set_last_oauth_error(str(exc))
        db = SessionLocal()
        try:
            add_event(db, "upwork", f"Login failed: {exc}")
            db.commit()
        finally:
            db.close()
    finally:
        flow.finished.set()


def _abandon_web_login(flow: WebOAuthFlow, message: str) -> None:
    if flow.task is not None:
        flow.task.cancel()
    flow.exception = TimeoutError(message)
    set_last_oauth_error(message)
    flow.finished.set()
    clear_web_oauth_flow()


@router.get("/upwork/connect")
async def upwork_connect(request: Request, db: Session = Depends(get_db)) -> Response:
    user = _user(request)
    force = request.query_params.get("force") == "1"
    client = UpworkMcpClient()
    if await client.is_authenticated() and not force:
        return RedirectResponse("/", status_code=303)

    flow = get_web_oauth_flow()
    if flow is not None and flow.redirect_url:
        return RedirectResponse(flow.redirect_url, status_code=303)
    if flow is not None and flow.exception:
        set_last_oauth_error(str(flow.exception))
        clear_web_oauth_flow()
        return RedirectResponse("/?oauth=failed", status_code=303)
    if flow is not None and flow.finished.is_set() and not flow.exception:
        clear_web_oauth_flow()
        return RedirectResponse("/?oauth=ok", status_code=303)
    if flow is not None and time.monotonic() - flow.started_at > 90:
        _abandon_web_login(flow, "Timed out starting Upwork OAuth")
        return RedirectResponse("/?oauth=timeout", status_code=303)
    if flow is None or flow.finished.is_set():
        flow = start_web_oauth_flow()
        flow.task = asyncio.create_task(_complete_web_login(flow))

    runtime = get_or_create_runtime(db)
    connect_path = "/upwork/connect?force=1" if force else "/upwork/connect"
    return render(
        request,
        "connecting.html",
        {
            "user": user,
            "counts": _counts(db),
            "runtime": runtime,
            "connect_path": connect_path,
            "status_message": "Contacting Upwork MCP and preparing the login URL…",
        },
    )


@router.get("/upwork/callback")
async def upwork_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    flow = get_web_oauth_flow()
    if flow is None:
        return RedirectResponse("/?oauth=missing", status_code=303)
    flow.payload = OAuthCallbackPayload(code=code, state=state, error=error)
    flow.code_ready.set()
    try:
        await asyncio.wait_for(flow.finished.wait(), 60)
    except TimeoutError:
        return RedirectResponse("/?oauth=timeout", status_code=303)
    exc = flow.exception
    clear_web_oauth_flow()
    if exc is not None:
        return RedirectResponse("/?oauth=failed", status_code=303)
    return RedirectResponse("/?oauth=ok", status_code=303)


@router.get("/history", response_class=HTMLResponse)
def history(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = _user(request)
    jobs = (
        db.query(Job)
        .filter(Job.status.in_([
            JobStatus.submitted.value,
            JobStatus.rejected.value,
            JobStatus.submit_failed.value,
            JobStatus.expired.value,
        ]))
        .order_by(Job.updated_at.desc())
        .limit(200)
        .all()
    )
    return render(
        request,
        "history.html",
        {"user": user, "jobs": jobs, "counts": _counts(db)},
    )


@router.post("/jobs/{job_id}/outcome")
async def job_outcome(
    job_id: int,
    outcome: str = Form(...),
    client_notes: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    job = db.query(Job).filter(Job.id == job_id).one_or_none()
    if job is None:
        return RedirectResponse("/", status_code=303)
    await log_interaction_feedback(str(job.id), outcome, client_notes, db=db)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/settings")
def settings_alias() -> Response:
    return RedirectResponse("/preferences", status_code=303)


def _opt_int(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _writer_from_form(
    opening_hook: str,
    enforce_opening_hook: str | None,
    tone: str,
    letter_structure: str,
    role_letter_structure: str,
    must_include: str,
    never_say: str,
    extra_instructions: str,
    target_words: str,
    milestone_instructions: str,
    ms_title: list[str],
    ms_weight: list[str],
    ms_description: list[str],
    milestone_min: str,
    milestone_max: str,
    screening_instructions: str,
    apply_questions_instructions: str,
    example_count: str,
) -> WriterConfig:
    stages: list[MilestoneStageConfig] = []
    count = max(len(ms_title), len(ms_weight), len(ms_description))
    for index in range(count):
        title = ms_title[index].strip() if index < len(ms_title) else ""
        description = ms_description[index].strip() if index < len(ms_description) else ""
        raw_weight = ms_weight[index].strip() if index < len(ms_weight) else ""
        if not title and not description and not raw_weight:
            continue
        try:
            weight = float(raw_weight.replace("%", ""))
        except ValueError:
            continue
        if not title:
            continue
        stages.append(MilestoneStageConfig(title=title, weight=weight, description=description))
    try:
        PitchTone(tone)
        tone_value = tone
    except ValueError:
        tone_value = PitchTone.consultative.value
    return WriterConfig(
        opening_hook=opening_hook.strip(),
        enforce_opening_hook=bool(enforce_opening_hook),
        tone=tone_value,
        letter_structure=letter_structure,
        role_letter_structure=role_letter_structure,
        must_include=must_include,
        never_say=never_say,
        extra_instructions=extra_instructions,
        target_words=_opt_int(target_words),
        milestone_instructions=milestone_instructions.strip(),
        milestone_stages=stages,
        milestone_min=_opt_int(milestone_min) or 3,
        milestone_max=_opt_int(milestone_max) or 5,
        screening_instructions=screening_instructions.strip(),
        apply_questions_instructions=apply_questions_instructions.strip(),
        example_count=max(0, _opt_int(example_count) or 0),
    )


@router.get("/proposal", response_class=HTMLResponse)
def proposal_page(request: Request, db: Session = Depends(get_db)) -> Response:
    user = _user(request)
    writer = load_proposal_settings(db)
    overlay = load_overlay(get_settings())
    examples = db.query(ProposalExample).order_by(ProposalExample.created_at.desc()).all()
    preview = build_system_prompt(writer, overlay, style_rules_for_prompt(db))
    return render(
        request,
        "proposal.html",
        {
            "user": user,
            "writer": writer,
            "examples": examples,
            "prompt_preview": preview,
            "tones": list(PitchTone),
            "counts": _counts(db),
            "stage_total": round(sum(item.weight for item in writer.milestone_stages), 1),
        },
    )


@router.post("/proposal/settings")
def save_proposal_settings(
    request: Request,
    opening_hook: str = Form(""),
    enforce_opening_hook: str | None = Form(default=None),
    tone: str = Form("consultative"),
    letter_structure: str = Form(""),
    role_letter_structure: str = Form(""),
    must_include: str = Form(""),
    never_say: str = Form(""),
    extra_instructions: str = Form(""),
    target_words: str = Form(""),
    milestone_instructions: str = Form(""),
    ms_title: list[str] = Form(default=[]),
    ms_weight: list[str] = Form(default=[]),
    ms_description: list[str] = Form(default=[]),
    milestone_min: str = Form("3"),
    milestone_max: str = Form("5"),
    screening_instructions: str = Form(""),
    apply_questions_instructions: str = Form(""),
    example_count: str = Form("2"),
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    row = get_or_create_proposal_settings(db)
    apply_config_to_row(
        row,
        _writer_from_form(
            opening_hook,
            enforce_opening_hook,
            tone,
            letter_structure,
            role_letter_structure,
            must_include,
            never_say,
            extra_instructions,
            target_words,
            milestone_instructions,
            ms_title,
            ms_weight,
            ms_description,
            milestone_min,
            milestone_max,
            screening_instructions,
            apply_questions_instructions,
            example_count,
        ),
    )
    return RedirectResponse("/proposal?saved=1", status_code=303)


@router.post("/proposal/reset")
def reset_proposal(request: Request, db: Session = Depends(get_db)) -> Response:
    _user(request)
    reset_proposal_settings(db)
    return RedirectResponse("/proposal?reset=1", status_code=303)


@router.post("/proposal/reset-role")
def reset_role_structure(request: Request, db: Session = Depends(get_db)) -> Response:
    _user(request)
    reset_role_letter_structure(db)
    return RedirectResponse("/proposal?reset=role", status_code=303)


@router.post("/proposal/examples")
def add_proposal_example(
    request: Request,
    title: str = Form(""),
    job_post: str = Form(""),
    cover_letter: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    if not job_post.strip() or not cover_letter.strip():
        return RedirectResponse("/proposal?error=example", status_code=303)
    db.add(
        ProposalExample(
            title=title.strip() or "Untitled example",
            job_post=job_post.strip(),
            cover_letter=cover_letter.strip(),
            notes=notes.strip(),
            active=True,
        )
    )
    return RedirectResponse("/proposal?example=1", status_code=303)


@router.post("/proposal/examples/{example_id}")
def update_proposal_example(
    request: Request,
    example_id: int,
    title: str = Form(""),
    job_post: str = Form(""),
    cover_letter: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    _user(request)
    row = db.query(ProposalExample).filter(ProposalExample.id == example_id).one_or_none()
    if row is None:
        return RedirectResponse("/proposal", status_code=303)
    row.title = title.strip() or row.title
    row.job_post = job_post.strip()
    row.cover_letter = cover_letter.strip()
    row.notes = notes.strip()
    return RedirectResponse("/proposal?example=1", status_code=303)


@router.post("/proposal/examples/{example_id}/delete")
def delete_proposal_example(request: Request, example_id: int, db: Session = Depends(get_db)) -> Response:
    _user(request)
    row = db.query(ProposalExample).filter(ProposalExample.id == example_id).one_or_none()
    if row is not None:
        db.delete(row)
    return RedirectResponse("/proposal", status_code=303)


@router.post("/proposal/examples/{example_id}/toggle")
def toggle_proposal_example(request: Request, example_id: int, db: Session = Depends(get_db)) -> Response:
    _user(request)
    row = db.query(ProposalExample).filter(ProposalExample.id == example_id).one_or_none()
    if row is not None:
        row.active = not row.active
    return RedirectResponse("/proposal", status_code=303)


@router.get("/preferences", response_class=HTMLResponse)
def preferences_page(request: Request, db: Session = Depends(get_db)) -> Response:
    user = _user(request)
    runtime = get_or_create_runtime(db)
    rules = db.query(PreferenceRule).order_by(PreferenceRule.created_at.desc()).all()
    overlay = load_overlay(get_settings())
    snapshot = db.query(UpworkProfile).order_by(UpworkProfile.id.asc()).first()
    upwork_skills: list[str] = []
    if snapshot is not None:
        try:
            parsed_skills = json.loads(snapshot.skills_json or "[]")
            if isinstance(parsed_skills, list):
                upwork_skills = [str(item) for item in parsed_skills if str(item).strip()]
        except json.JSONDecodeError:
            upwork_skills = []
    return render(
        request,
        "preferences.html",
        {
            "user": user,
            "runtime": runtime,
            "rules": rules,
            "counts": _counts(db),
            "overlay": overlay,
            "upwork_profile": snapshot,
            "upwork_skills": upwork_skills,
        },
    )


@router.post("/preferences/runtime")
def save_runtime(
    autonomy_mode: str = Form(...),
    auto_submit_threshold: int = Form(85),
    min_score: int = Form(70),
    min_hourly: str = Form(""),
    min_fixed: str = Form(""),
    min_client_rating: str = Form(""),
    min_client_hires: str = Form(""),
    max_proposal_count: str = Form(""),
    prefer_timezones: str = Form(""),
    require_verified_payment: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> Response:
    runtime = get_or_create_runtime(db)
    runtime.autonomy_mode = autonomy_mode
    runtime.auto_submit_threshold = auto_submit_threshold
    runtime.min_score = min_score
    runtime.min_hourly = _opt_int(min_hourly)
    runtime.min_fixed = _opt_int(min_fixed)
    runtime.require_verified_payment = bool(require_verified_payment)
    try:
        runtime.min_client_rating = float(min_client_rating) if min_client_rating.strip() else None
    except ValueError:
        runtime.min_client_rating = None
    runtime.min_client_hires = _opt_int(min_client_hires)
    runtime.max_proposal_count = _opt_int(max_proposal_count)
    runtime.prefer_timezones = prefer_timezones.strip()
    apply_runtime_filters(db)
    return RedirectResponse("/preferences", status_code=303)


@router.post("/preferences/rules")
async def add_rule(
    category: str = Form(...),
    rule: str = Form(...),
    enforcement_level: str = Form("soft_penalty"),
    db: Session = Depends(get_db),
) -> Response:
    await learn_preference(category, rule, enforcement_level, db=db)
    apply_runtime_filters(db)
    return RedirectResponse("/preferences", status_code=303)


@router.post("/preferences/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int, db: Session = Depends(get_db)) -> Response:
    row = db.query(PreferenceRule).filter(PreferenceRule.id == rule_id).one_or_none()
    if row is not None:
        row.active = not row.active
    apply_runtime_filters(db)
    return RedirectResponse("/preferences", status_code=303)


@router.post("/preferences/rules/{rule_id}/delete")
def delete_rule(rule_id: int, db: Session = Depends(get_db)) -> Response:
    row = db.query(PreferenceRule).filter(PreferenceRule.id == rule_id).one_or_none()
    if row is not None:
        db.delete(row)
    apply_runtime_filters(db)
    return RedirectResponse("/preferences", status_code=303)


@router.post("/preferences/overlay")
def save_profile_overlay(
    name: str = Form(""),
    title: str = Form(""),
    hourly_rate: str = Form(""),
    working_hours: str = Form(""),
    voice: str = Form(""),
    skills: str = Form(""),
    exclude_keywords: str = Form(""),
    search_queries: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    rate = int(hourly_rate) if hourly_rate.strip().isdigit() else None
    overlay = FreelancerProfile(
        name=name.strip(),
        title=title.strip(),
        hourly_rate=rate,
        working_hours=working_hours.strip(),
        voice=voice.strip() or "concise, specific, no fluff",
        skills=[part.strip() for part in skills.split(",") if part.strip()],
        exclude_keywords=[part.strip() for part in exclude_keywords.split(",") if part.strip()],
        search_queries=[part.strip() for part in search_queries.split("\n") if part.strip()],
    )
    save_overlay(overlay)
    apply_runtime_filters(db)
    return RedirectResponse("/preferences", status_code=303)


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request, db: Session = Depends(get_db)) -> Response:
    user = _user(request)
    items = db.query(PortfolioItem).order_by(PortfolioItem.created_at.desc()).all()
    parsed = []
    for item in items:
        try:
            stack = json.loads(item.tech_stack)
        except json.JSONDecodeError:
            stack = []
        try:
            keywords = json.loads(item.associated_keywords)
        except json.JSONDecodeError:
            keywords = []
        parsed.append(
            {
                "id": item.id,
                "project_title": item.project_title,
                "tech_stack": stack if isinstance(stack, list) else [],
                "outcomes_achieved": item.outcomes_achieved,
                "associated_keywords": keywords if isinstance(keywords, list) else [],
                "description": item.description or "",
                "origin": item.origin,
                "kind": item.kind,
                "editable": item.origin == WorkOrigin.agent.value,
            }
        )
    upwork_items = [item for item in parsed if item["origin"] == WorkOrigin.upwork.value]
    upwork_work = [item for item in upwork_items if item["kind"] != WorkKind.proposal.value]
    upwork_proposals = [item for item in upwork_items if item["kind"] == WorkKind.proposal.value]
    agent_items = [item for item in parsed if item["origin"] != WorkOrigin.upwork.value]
    return render(
        request,
        "portfolio.html",
        {
            "user": user,
            "upwork_items": upwork_work,
            "upwork_proposals": upwork_proposals,
            "agent_items": agent_items,
            "counts": _counts(db),
        },
    )


@router.post("/portfolio")
async def add_portfolio(
    project_title: str = Form(...),
    tech_stack: str = Form(""),
    outcomes_achieved: str = Form(""),
    associated_keywords: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    stack = [part.strip() for part in tech_stack.split(",") if part.strip()]
    keys = [part.strip() for part in associated_keywords.split(",") if part.strip()]
    await update_portfolio_matrix(
        project_title,
        stack,
        outcomes_achieved,
        keys,
        description=description,
        db=db,
    )
    return RedirectResponse("/portfolio", status_code=303)


@router.post("/portfolio/sync-upwork")
async def sync_upwork_portfolio(db: Session = Depends(get_db)) -> Response:
    try:
        await sync_upwork_memory(db)
    except Exception as exc:
        add_event(db, "upwork", f"Sync failed: {exc}")
    return RedirectResponse("/portfolio", status_code=303)


@router.post("/portfolio/{item_id}/edit")
async def edit_portfolio(
    item_id: int,
    project_title: str = Form(...),
    tech_stack: str = Form(""),
    outcomes_achieved: str = Form(""),
    associated_keywords: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    stack = [part.strip() for part in tech_stack.split(",") if part.strip()]
    keys = [part.strip() for part in associated_keywords.split(",") if part.strip()]
    await save_agent_item(
        item_id,
        project_title,
        stack,
        outcomes_achieved,
        keys,
        description=description,
        db=db,
    )
    return RedirectResponse("/portfolio", status_code=303)


@router.post("/portfolio/{item_id}/delete")
def delete_portfolio(item_id: int, db: Session = Depends(get_db)) -> Response:
    row = db.query(PortfolioItem).filter(PortfolioItem.id == item_id).one_or_none()
    if row is not None and row.origin == WorkOrigin.agent.value:
        db.delete(row)
    return RedirectResponse("/portfolio", status_code=303)


def _read_uploads(items: list[UploadFile] | None) -> list[tuple[str, bytes, str]]:
    files: list[tuple[str, bytes, str]] = []
    for item in items or []:
        name = (item.filename or "").strip()
        if not name:
            continue
        data = item.file.read()
        if not data:
            continue
        files.append((name, data, item.content_type or "application/octet-stream"))
    return files


def _safe_room_next(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("room_") and "/" not in text and "?" not in text:
        return f"/messages/{text}"
    return "/messages"


async def _messages_context(
    request: Request,
    db: Session,
    selected_room_id: str | None = None,
) -> dict:
    user = _user(request)
    rooms = load_rooms(db)
    cards = [thread_card(room, related=related_job_id(db, room)) for room in rooms]
    mcp = UpworkMcpClient()
    connected = await mcp.is_authenticated()
    selected = None
    messages: list = []
    if selected_room_id:
        for card in cards:
            if card["room_id"] == selected_room_id:
                selected = card
                break
    q_error = request.query_params.get("error") or ""
    if selected is not None:
        if q_error == "first":
            selected["client_first_required"] = True
            selected["can_send"] = False
        room_row = next((row for row in rooms if row.room_id == selected_room_id), None)
        if room_row is not None:
            rows = db.query(ChatMessage).filter(ChatMessage.room_pk == room_row.id).all()
            messages = message_views(rows)
    return {
        "user": user,
        "rooms": cards,
        "room": selected,
        "messages": messages,
        "counts": _counts(db),
        "connected": connected,
        "error": q_error,
        "synced": request.query_params.get("synced") or "",
        "notice": request.query_params.get("sent") or request.query_params.get("draft") or "",
    }


@router.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request, db: Session = Depends(get_db)) -> Response:
    return render(request, "messages.html", await _messages_context(request, db))


@router.post("/messages/sync")
async def messages_sync(next: str = Form(""), db: Session = Depends(get_db)) -> Response:
    dest = _safe_room_next(next)
    mcp = UpworkMcpClient()
    if not await mcp.is_authenticated():
        return RedirectResponse(f"{dest}?error=connect", status_code=303)
    try:
        await sync_messages(mcp, db)
    except Exception as exc:
        detail = format_mcp_error(exc)
        if oauth_needs_login(detail):
            return RedirectResponse(f"{dest}?error=connect", status_code=303)
        logger.exception("messages sync failed")
        return RedirectResponse(f"{dest}?error=sync", status_code=303)
    return RedirectResponse(f"{dest}?synced=1", status_code=303)


@router.get("/messages/{room_id}", response_class=HTMLResponse)
async def message_thread(request: Request, room_id: str, db: Session = Depends(get_db)) -> Response:
    exists = db.query(MessageRoom).filter(MessageRoom.room_id == room_id).one_or_none()
    if exists is None:
        return RedirectResponse("/messages", status_code=303)
    return render(request, "messages.html", await _messages_context(request, db, room_id))


@router.post("/messages/{room_id}/suggest")
async def suggest_reply(room_id: str, db: Session = Depends(get_db)) -> Response:
    room = db.query(MessageRoom).filter(MessageRoom.room_id == room_id).one_or_none()
    if room is None:
        return RedirectResponse("/messages", status_code=303)
    rows = db.query(ChatMessage).filter(ChatMessage.room_pk == room.id).all()
    views = message_views(rows)
    lines = []
    for item in views:
        who = "You" if item.get("sender") == "you" else ("System" if item.get("sender") == "system" else "Client")
        lines.append(f"{who}: {item.get('body') or ''}")
    try:
        result = llm_suggest_reply(
            "\n\n".join(lines) or "(empty thread)",
            load_profile(),
            get_settings(),
            counterpart=room.counterpart or room.title,
        )
    except Exception:
        logger.exception("suggest reply failed")
        return RedirectResponse(f"/messages/{room.room_id}?error=suggest", status_code=303)
    save_suggested_draft(db, room, result.intents[0].text, result.intents)
    return RedirectResponse(f"/messages/{room.room_id}?draft=1", status_code=303)


@router.post("/messages/{room_id}/send")
async def send_reply(
    room_id: str,
    body: str = Form(""),
    chat_files: list[UploadFile] | None = File(default=None),
    db: Session = Depends(get_db),
) -> Response:
    room = db.query(MessageRoom).filter(MessageRoom.room_id == room_id).one_or_none()
    if room is None:
        return RedirectResponse("/messages", status_code=303)
    if room.send_status == "sending":
        return RedirectResponse(f"/messages/{room.room_id}", status_code=303)
    text = body.strip()
    uploads = _read_uploads(chat_files)
    if not text and not uploads:
        return RedirectResponse(f"/messages/{room.room_id}?error=empty", status_code=303)
    if len(text) > 10240:
        return RedirectResponse(f"/messages/{room.room_id}?error=long", status_code=303)
    room.send_status = "sending"
    room.send_error = None
    db.add(room)
    db.commit()
    mcp = UpworkMcpClient()
    try:
        if not await mcp.is_authenticated():
            raise RuntimeError("Upwork is not connected")
        attachments: list[dict[str, str]] = []
        if uploads:
            attachments = await mcp.upload_files(uploads, context="messages", room_id=room.room_id)
        result = await mcp.send_room_message(room.room_id, text, attachments)
        room.send_status = "idle"
        room.send_error = None
        db.add(room)
        db.commit()
        try:
            await refresh_room(mcp, db, room)
        except Exception:
            logger.exception("refresh after send failed")
        add_event(db, "messages", f"sent room={room.room_id} {result[:180]}")
        return RedirectResponse(f"/messages/{room.room_id}?sent=1", status_code=303)
    except Exception as exc:
        detail = format_mcp_error(exc)
        room.send_status = "error"
        room.send_error = detail
        db.add(room)
        db.commit()
        if first_message_blocked(detail):
            return RedirectResponse(f"/messages/{room.room_id}?error=first", status_code=303)
        if oauth_needs_login(detail):
            return RedirectResponse(f"/messages/{room.room_id}?error=connect", status_code=303)
        logger.exception("send message failed")
        return RedirectResponse(f"/messages/{room.room_id}?error=send", status_code=303)

