from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from app.config import Settings, get_settings
from app.db.models import FeedbackLog, Job, UpworkApplication
from app.db.session import SessionLocal
from app.events import add_event
from app.models import JobPayload, JobStatus
from app.profile import load_profile
from app.runtime import load_runtime
from app.tools.discovery import (
    _parse_client,
    client_hard_gate_reasons,
    execute_client_score,
    execute_scoring_matrix,
    fetch_live_jobs,
    job_filter_fields,
    persist_client_score,
    settings_block_reasons_for_job,
)
from app.upwork.mcp_client import (
    UpworkMcpClient,
    derive_client_stats,
    fold_search_client,
    format_mcp_error,
    merge_client_details,
    oauth_needs_login,
    prefer_price_label,
    price_from_raw,
    public_job_url,
    job_ref_keys,
)
from app.upwork.messages import sync_messages
from app.upwork.outcomes import application_for_posting
from app.upwork.sync import sync_proposal_outcomes

logger = logging.getLogger(__name__)


def _details_dict(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _needs_client_refresh(row: Job) -> bool:
    data = _details_dict(row.client_json)
    if not data:
        return True
    attachments = data.get("attachments")
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict) and item.get("url") and not item.get("path"):
                return True
    if data.get("job_details_fetched"):
        return False
    return "invites_sent" not in data


def _job_url(payload: JobPayload) -> str | None:
    if payload.get("url"):
        return public_job_url(payload["url"])
    job_id = payload.get("id")
    if not job_id:
        return None
    return public_job_url(job_id)


def expire_stale_jobs() -> int:
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        stale = (
            db.query(Job)
            .filter(
                Job.status == JobStatus.pending_review.value,
                Job.expires_at.is_not(None),
                Job.expires_at < now,
            )
            .all()
        )
        for job in stale:
            job.status = JobStatus.expired.value
            add_event(db, "expired", "No action before expiry", job.id)
            db.add(
                FeedbackLog(
                    job_id=job.id,
                    upwork_id=job.upwork_id,
                    outcome="expired",
                    client_notes="No action before expiry",
                )
            )
        db.commit()
        return len(stale)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def backfill_job_details(mcp: UpworkMcpClient) -> None:
    db = SessionLocal()
    try:
        rows = db.query(Job).filter(Job.status == JobStatus.pending_review.value).all()
        missing = [row for row in rows if _needs_client_refresh(row)][:20]
        ids = [row.upwork_id for row in missing]
    finally:
        db.close()
    by_id: dict[str, JobPayload] = {}
    if ids:
        enriched = await mcp.enrich_jobs([{"id": job_id} for job_id in ids])
        by_id = {item["id"]: item for item in enriched if item.get("id")}
    db = SessionLocal()
    try:
        applied: set[str] = set()
        for item in db.query(UpworkApplication).all():
            applied |= job_ref_keys(item.posting_id)
        pending = db.query(Job).filter(Job.status == JobStatus.pending_review.value).all()
        runtime = load_runtime(db, mcp.settings)
        for row in pending:
            extra = by_id.get(row.upwork_id, {})
            try:
                raw = json.loads(row.raw_json or "{}")
            except json.JSONDecodeError:
                raw = {}
            search_price = price_from_raw(raw) if isinstance(raw, dict) else ""
            row.price_label = prefer_price_label(
                row.price_label,
                extra.get("price_label"),
                extra.get("budget"),
                search_price,
            )
            row.budget = row.price_label or extra.get("budget") or row.budget
            row.timezone = extra.get("timezone") or row.timezone
            row.job_type = extra.get("job_type") or row.job_type
            try:
                left = json.loads(row.client_json or "{}")
                info = json.loads(row.client_info or "{}")
                right = json.loads(extra.get("client_details") or "{}")
                merged = left if isinstance(left, dict) else {}
                if isinstance(info, dict):
                    merged = fold_search_client(merged, info)
                if isinstance(right, dict):
                    merged = merge_client_details(merged, right)
                derive_client_stats(merged)
                if merged:
                    row.client_json = json.dumps(merged, default=str)
            except json.JSONDecodeError:
                row.client_json = extra.get("client_details") or row.client_json
            persist_client_score(
                row,
                execute_client_score(_details_dict(row.client_json), runtime, settings=mcp.settings),
            )
            reasons = settings_block_reasons_for_job(row, db, mcp.settings)
            if reasons:
                row.status = JobStatus.skipped.value
                row.expires_at = None
                note = "skipped by settings: " + "; ".join(reasons)
                row.score_reason = note + (f"; {row.score_reason}" if row.score_reason else "")
                add_event(db, "skipped", note, row.id)
            if any(job_ref_keys(row.upwork_id) & applied) or row.status == JobStatus.submitted.value:
                row.applied_on_upwork = True
            extra_desc = extra.get("description") or ""
            if len(extra_desc) > len(row.description or ""):
                row.description = extra_desc
        db.commit()
    finally:
        db.close()


async def ingest_payload(payload: JobPayload, settings: Settings, mcp: UpworkMcpClient) -> str:
    job_id = payload.get("id")
    if not job_id:
        return "ignored"
    db = SessionLocal()
    try:
        existing = db.query(Job).filter(Job.upwork_id == job_id).one_or_none()
        if existing is not None:
            return "duplicate"
        detailed: JobPayload = dict(payload)
        rating = detailed.get("client_rating")
        payment = detailed.get("client_payment_status") or ""
        budget_value = detailed.get("job_budget_value")
        duration = detailed.get("estimated_duration") or ""
        if rating is None or not payment or budget_value is None:
            parsed_rating, parsed_payment, parsed_budget, parsed_duration = _parse_client(detailed)
            rating = rating if rating is not None else parsed_rating
            payment = payment or parsed_payment
            budget_value = budget_value if budget_value is not None else parsed_budget
            duration = duration or parsed_duration
        details = _details_dict(detailed.get("client_details") or detailed.get("client"))
        derive_client_stats(details)
        detailed["client_details"] = json.dumps(details, default=str)
        attachment_text = str(details.get("attachment_text") or "")
        quals = details.get("preferred_qualifications") if isinstance(details.get("preferred_qualifications"), dict) else {}
        contractor_type = str(quals.get("contractor_type") or "")
        hire_rate = details.get("hire_rate")
        invites_sent = details.get("invites_sent")
        interviewing = details.get("interviewing")
        job_text = "\n".join(part for part in (detailed.get("description") or "", attachment_text) if part)
        fields = job_filter_fields(details)
        scored = await execute_scoring_matrix(
            client_rating=rating,
            client_payment_status=payment,
            job_budget=budget_value,
            estimated_duration=duration,
            job_text=job_text,
            title=detailed.get("title") or "",
            timezone=detailed.get("timezone") or "",
            job_type=detailed.get("job_type") or "",
            client_hires=detailed.get("client_hires"),
            proposal_count=detailed.get("proposal_count"),
            hire_rate=float(hire_rate) if isinstance(hire_rate, (int, float)) else None,
            invites_sent=int(invites_sent) if isinstance(invites_sent, (int, float)) else None,
            interviewing=int(interviewing) if isinstance(interviewing, (int, float)) else None,
            attachment_text=attachment_text,
            price_label=str(detailed.get("price_label") or detailed.get("budget") or ""),
            contractor_type=contractor_type,
            experience_level=fields["experience_level"],
            client_country=fields["client_country"],
            client_spend=fields["client_spend"],
            connects_cost=fields["connects_cost"],
            db=db,
            settings=settings,
        )
        runtime = load_runtime(db, settings)
        client_scored = execute_client_score(details, runtime, settings=settings)
        hires = detailed.get("client_hires")
        if not isinstance(hires, (int, float)):
            hires = details.get("hires") if isinstance(details.get("hires"), (int, float)) else None
        else:
            hires = int(hires)
        client_gates = client_hard_gate_reasons(
            runtime,
            payment_status=payment,
            rating=rating,
            hires=int(hires) if isinstance(hires, (int, float)) else None,
            client_country=fields["client_country"],
            client_spend=fields["client_spend"],
        )
        go = scored.go and client_scored.score >= runtime.min_client_score and not client_gates
        status = JobStatus.pending_review if go else JobStatus.skipped
        applied_row = application_for_posting(db, job_id)
        job = Job(
            upwork_id=job_id,
            title=detailed.get("title") or "Untitled job",
            description=detailed.get("description") or "",
            budget=detailed.get("price_label") or detailed.get("budget"),
            url=_job_url(detailed),
            client_info=detailed.get("client"),
            raw_json=detailed.get("raw"),
            score=scored.score,
            score_reason=scored.reason,
            score_breakdown=json.dumps(scored.breakdown),
            client_score=client_scored.score,
            client_score_reason=client_scored.reason,
            client_score_breakdown=json.dumps(client_scored.breakdown),
            status=status.value,
            price_label=detailed.get("price_label") or detailed.get("budget"),
            timezone=detailed.get("timezone") or "",
            job_type=detailed.get("job_type") or "",
            client_json=detailed.get("client_details") or detailed.get("client"),
            applied_on_upwork=applied_row is not None,
            expires_at=(
                datetime.now(UTC) + timedelta(hours=settings.approval_ttl_hours)
                if status == JobStatus.pending_review
                else None
            ),
        )
        db.add(job)
        db.flush()
        add_event(db, "scored", f"{scored.score}/{client_scored.score}: {scored.reason}", job.id)
        db.commit()
        if not go:
            return "skipped"
        if applied_row is not None:
            return "skipped"
        return "queued"
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_agent_cycle(settings: Settings | None = None) -> dict[str, int]:
    settings = settings or get_settings()
    counts = {"searched": 0, "new": 0, "queued": 0, "skipped": 0, "submitted": 0, "expired": 0, "errors": 0}
    counts["expired"] = expire_stale_jobs()
    mcp = UpworkMcpClient(settings)
    if not await mcp.is_authenticated():
        db = SessionLocal()
        try:
            add_event(db, "poll", "Skipped: Upwork MCP not logged in")
            db.commit()
        finally:
            db.close()
        return counts
    profile = load_profile(settings)
    queries = profile.search_queries or [
        q.strip() for q in settings.search_queries.split(",") if q.strip()
    ]
    for query in queries:
        try:
            jobs = await fetch_live_jobs(query, client=mcp)
        except Exception as exc:
            counts["errors"] += 1
            detail = format_mcp_error(exc)
            logger.exception("poll search failed for %s", query)
            db = SessionLocal()
            try:
                add_event(db, "poll_error", f"{query}: {detail}")
                db.commit()
            finally:
                db.close()
            if oauth_needs_login(detail):
                db = SessionLocal()
                try:
                    add_event(db, "poll", "Upwork session expired. Connect Upwork from the inbox.")
                    db.commit()
                finally:
                    db.close()
                break
            continue
        counts["searched"] += len(jobs)
        db = SessionLocal()
        try:
            known = {row[0] for row in db.query(Job.upwork_id).all()}
        finally:
            db.close()
        fresh = [item for item in jobs if item.get("id") not in known]
        if fresh:
            try:
                fresh = await mcp.enrich_jobs(fresh)
            except Exception:
                logger.exception("job detail enrich failed")
        for payload in fresh:
            try:
                result = await ingest_payload(payload, settings, mcp)
            except Exception:
                logger.exception("ingest failed")
                counts["errors"] += 1
                continue
            if result == "duplicate":
                continue
            counts["new"] += 1
            if result == "queued":
                counts["queued"] += 1
            elif result == "skipped":
                counts["skipped"] += 1
            elif result == "submitted":
                counts["submitted"] += 1
    try:
        await backfill_job_details(mcp)
    except Exception:
        logger.exception("backfill job details failed")
    try:
        db_msg = SessionLocal()
        skip_outcomes = False
        try:
            try:
                n = await sync_messages(mcp, db_msg)
                add_event(db_msg, "messages", f"synced_rooms={n}")
                db_msg.commit()
            except Exception as exc:
                db_msg.rollback()
                detail = format_mcp_error(exc)
                logger.exception("message sync failed")
                add_event(db_msg, "poll_error", f"messages: {detail}")
                db_msg.commit()
                if oauth_needs_login(detail):
                    add_event(db_msg, "poll", "Upwork session expired. Connect Upwork from the inbox.")
                    db_msg.commit()
                    skip_outcomes = True
            if not skip_outcomes:
                try:
                    outcome_counts = await sync_proposal_outcomes(mcp, db_msg)
                    add_event(
                        db_msg,
                        "outcomes",
                        (
                            f"applications={outcome_counts['applications']} "
                            f"logged={outcome_counts['logged']}"
                        ),
                    )
                    db_msg.commit()
                except Exception as exc:
                    db_msg.rollback()
                    detail = format_mcp_error(exc)
                    logger.exception("proposal outcome sync failed")
                    add_event(db_msg, "poll_error", f"outcomes: {detail}")
                    db_msg.commit()
                    if oauth_needs_login(detail):
                        add_event(db_msg, "poll", "Upwork session expired. Connect Upwork from the inbox.")
                        db_msg.commit()
        finally:
            db_msg.close()
    except Exception:
        logger.exception("message sync wrapper failed")
    db = SessionLocal()
    try:
        add_event(
            db,
            "poll",
            (
                f"searched={counts['searched']} new={counts['new']} "
                f"queued={counts['queued']} skipped={counts['skipped']} "
                f"submitted={counts['submitted']} errors={counts['errors']}"
            ),
        )
        db.commit()
    finally:
        db.close()
    return counts
