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
from app.runtime import load_runtime, should_auto_submit
from app.tools.discovery import _parse_client, execute_scoring_matrix, fetch_live_jobs
from app.tools.execution import PitchSkipped, generate_tailored_pitch, submit_proposal
from app.upwork.mcp_client import (
    UpworkMcpClient,
    derive_client_stats,
    fold_search_client,
    merge_client_details,
    prefer_price_label,
    price_from_raw,
)

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
        return payload["url"]
    job_id = payload.get("id")
    if not job_id:
        return None
    if job_id.startswith("http"):
        return job_id
    return f"https://www.upwork.com/jobs/{job_id}"


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
        applied = {row.posting_id for row in db.query(UpworkApplication).all()}
        pending = db.query(Job).filter(Job.status == JobStatus.pending_review.value).all()
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
            row.applied_on_upwork = row.upwork_id in applied or row.status == JobStatus.submitted.value
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
        hire_rate = details.get("hire_rate")
        invites_sent = details.get("invites_sent")
        interviewing = details.get("interviewing")
        job_text = "\n".join(part for part in (detailed.get("description") or "", attachment_text) if part)
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
            db=db,
            settings=settings,
        )
        runtime = load_runtime(db, settings)
        status = JobStatus.pending_review if scored.go else JobStatus.skipped
        applied_row = db.query(UpworkApplication).filter(UpworkApplication.posting_id == job_id).one_or_none()
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
        add_event(db, "scored", f"{scored.score}: {scored.reason}", job.id)
        job_pk = job.id
        db.commit()
        if not scored.go:
            return "skipped"
        if applied_row is not None:
            return "skipped"
        try:
            drafted = await generate_tailored_pitch(str(job_pk), settings=settings)
        except PitchSkipped:
            return "skipped"
        except Exception:
            logger.exception("draft failed for job %s", job_pk)
            return "queued"
        db2 = SessionLocal()
        try:
            stored = db2.query(Job).filter(Job.id == job_pk).one()
            stored.matched_context = json.dumps(drafted.matched_context)
            stored.description = detailed.get("description") or stored.description
            add_event(db2, "funnel", "pitch_drafted", stored.id)
            if should_auto_submit(scored.score, runtime):
                await submit_proposal(
                    str(stored.id),
                    drafted.cover_letter,
                    db=db2,
                    screening_answers=drafted.screening_answers,
                    portfolio_project_ids=drafted.portfolio_project_ids,
                    certificate_ids=drafted.certificate_ids,
                )
                db2.commit()
                return "submitted"
            db2.commit()
            return "queued"
        except Exception:
            db2.rollback()
            logger.exception("failed to store draft for job %s", job_pk)
            return "queued"
        finally:
            db2.close()
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
            db = SessionLocal()
            try:
                add_event(db, "poll_error", f"{query}: {exc}")
                db.commit()
            finally:
                db.close()
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
