from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import FeedbackLog, Job, PreferenceRule
from app.db.session import SessionLocal
from app.embeddings import query_similar
from app.eligibility import hard_block_reasons
from app.engagement import classify_engagement
from app.events import add_event
from app.models import (
    ContextMatch,
    EngagementFilter,
    FetchLiveJobsArgs,
    FreelancerProfile,
    JobFilterFields,
    JobPayload,
    JobStatus,
    JobTypeFilter,
    RetrieveContextArgs,
    RuntimeSettings,
    ScoreResult,
    ScoringMatrixArgs,
    ToolSpec,
)
from app.profile import load_profile
from app.runtime import load_runtime
from app.scoring import load_scoring_config
from app.tools import register_tool
from app.upwork.mcp_client import UpworkMcpClient

_STOP = {
    "never",
    "always",
    "apply",
    "jobs",
    "job",
    "requiring",
    "require",
    "required",
    "with",
    "from",
    "that",
    "this",
    "have",
    "has",
    "the",
    "and",
    "for",
    "not",
}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]{1,}", re.I)
_FIXED_PRICE = re.compile(r"\$?\s*(\d+(?:\.\d+)?)\s*fixed", re.I)
_HOURLY_RANGE = re.compile(r"\$?\s*(\d+(?:\.\d+)?)\s*[–\-]\s*\$?\s*(\d+(?:\.\d+)?)\s*/\s*hr", re.I)
_HOURLY_ONE = re.compile(r"\$?\s*(\d+(?:\.\d+)?)\s*\+?\s*/\s*hr", re.I)
_NO_LISTED_BUDGET = re.compile(r"no range|not provided|not_provided|range set", re.I)


def _rule_matches(rule_text: str, blob: str) -> bool:
    needle = rule_text.lower().strip()
    if len(needle) <= 40 and needle in blob:
        return True
    keywords = [tok for tok in _tokens(needle) if len(tok) > 3 and tok not in _STOP]
    if not keywords:
        return needle in blob
    hits = [tok for tok in keywords if tok in blob or tok.rstrip("s") in blob]
    return len(hits) >= min(2, len(keywords))


def _session(db: Session | None) -> tuple[Session, bool]:
    if db is not None:
        return db, False
    return SessionLocal(), True


def _tokens(text: str) -> set[str]:
    return {tok.lower().strip(".-#+") for tok in _TOKEN_RE.findall(text or "") if tok.lower().strip(".-#+")}


def infer_job_kind(job_type: str, price_label: str = "") -> str:
    kind = (job_type or "").lower().strip()
    if kind in {"fixed", "fixed_price"}:
        return "fixed"
    if kind in {"hourly", "hourly_job"}:
        return "hourly"
    blob = (price_label or "").lower()
    if "fixed" in blob:
        return "fixed"
    if "/hr" in blob or "hourly" in blob:
        return "hourly"
    return kind


def parse_price_amount(job_type: str, price_label: str, budget: float | None) -> tuple[str, float | None]:
    kind = infer_job_kind(job_type, price_label)
    text = (price_label or "").strip()
    if _NO_LISTED_BUDGET.search(text) or text.lower() in {"hourly", "fixed"}:
        return kind, None
    if kind == "hourly":
        ranged = _HOURLY_RANGE.search(text)
        if ranged:
            return "hourly", float(ranged.group(1))
        one = _HOURLY_ONE.search(text)
        if one:
            return "hourly", float(one.group(1))
        if budget is not None and budget > 0:
            return "hourly", float(budget)
        return "hourly", None
    if kind == "fixed":
        match = _FIXED_PRICE.search(text)
        if match:
            return "fixed", float(match.group(1))
        if "$" in text:
            numbers = re.findall(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
            if numbers:
                return "fixed", float(numbers[0])
        if budget is not None and budget > 0:
            return "fixed", float(budget)
        return "fixed", None
    if budget is not None and budget > 0:
        return kind, float(budget)
    return kind, None


_ENTRY_KEYS = {"entry", "entry_level", "entrylevel", "beginner", "intern", "internship"}


def _client_dict(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _money_amount(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return _money_amount(value.get("rawValue") or value.get("amount") or value.get("displayValue"))
    try:
        return float(str(value).replace("$", "").replace(",", "").split()[0])
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    amount = _money_amount(value)
    if amount is None:
        return None
    return int(amount)


def _is_entry_level(experience_level: str) -> bool:
    key = re.sub(r"[^a-z0-9]+", "_", (experience_level or "").lower()).strip("_")
    if not key:
        return False
    return key in _ENTRY_KEYS or key.startswith("entry")


def _country_blocked(country: str, blocked: str) -> bool:
    hay = (country or "").lower().strip()
    if not hay:
        return False
    for token in blocked.split(","):
        needle = token.strip().lower()
        if needle and needle in hay:
            return True
    return False


def job_filter_fields(data: dict[str, Any]) -> JobFilterFields:
    return {
        "experience_level": str(data.get("experience_level") or ""),
        "client_country": str(data.get("country") or ""),
        "client_spend": _money_amount(data.get("spend_total")),
        "connects_cost": _as_int(data.get("connects_cost")),
    }


def structured_skip_reasons(
    runtime: RuntimeSettings,
    *,
    kind: str,
    experience_level: str = "",
    client_country: str = "",
    client_spend: float | None = None,
    connects_cost: int | None = None,
    title: str = "",
    description: str = "",
    job_type: str = "",
) -> list[str]:
    reasons: list[str] = []
    if runtime.skip_entry_level and _is_entry_level(experience_level):
        reasons.append("entry-level job")
    wanted_type = runtime.job_type_filter
    if wanted_type != JobTypeFilter.any and kind in {"hourly", "fixed"} and kind != wanted_type.value:
        reasons.append(f"job type {kind} not {wanted_type.value}")
    wanted_eng = runtime.engagement_filter
    if wanted_eng != EngagementFilter.any:
        engagement = classify_engagement(title, description, job_type or kind)
        if engagement != wanted_eng.value:
            reasons.append(f"engagement {engagement} not {wanted_eng.value}")
    if _country_blocked(client_country, runtime.blocked_client_countries):
        reasons.append(f"blocked country {client_country}")
    if runtime.min_client_spend is not None and client_spend is not None and client_spend < runtime.min_client_spend:
        reasons.append(f"client spend {client_spend:g} below {runtime.min_client_spend}")
    if runtime.max_connects_cost is not None and connects_cost is not None and connects_cost > runtime.max_connects_cost:
        reasons.append(f"connects {connects_cost} above {runtime.max_connects_cost}")
    return reasons


def _job_client_fields(job: Job) -> tuple[str, float | None, int | None]:
    raw = job.client_json or job.client_info or ""
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    verified = data.get("verified")
    if verified is True:
        payment = "verified"
    elif verified is False:
        payment = "unverified"
    else:
        payment = str(data.get("payment_status") or "")
    rating: float | None = None
    if data.get("rating") not in (None, ""):
        try:
            rating = float(data["rating"])
        except (TypeError, ValueError):
            rating = None
    hires: int | None = None
    if data.get("hires") not in (None, ""):
        try:
            hires = int(data["hires"])
        except (TypeError, ValueError):
            hires = None
    return payment, rating, hires


def _eligibility_fields(job: Job) -> tuple[str, str]:
    try:
        data = json.loads(job.client_json or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    attachment = str(data.get("attachment_text") or "")
    quals = data.get("preferred_qualifications") if isinstance(data.get("preferred_qualifications"), dict) else {}
    contractor = str(quals.get("contractor_type") or "") if isinstance(quals, dict) else ""
    return attachment, contractor


def hard_gate_reasons(
    runtime: RuntimeSettings,
    profile: FreelancerProfile,
    rules: list[PreferenceRule],
    *,
    kind: str,
    amount: float | None,
    score: int | None,
    payment_status: str,
    rating: float | None,
    hires: int | None,
    blob: str,
    contractor_type: str = "",
    experience_level: str = "",
    client_country: str = "",
    client_spend: float | None = None,
    connects_cost: int | None = None,
    title: str = "",
    description: str = "",
    job_type: str = "",
) -> list[str]:
    reasons: list[str] = []
    if score is not None and score < runtime.min_score:
        reasons.append(f"score {score} below {runtime.min_score}")
    min_hourly = runtime.min_hourly if runtime.min_hourly is not None else profile.hourly_rate
    if kind == "fixed" and runtime.min_fixed and amount is not None and amount < runtime.min_fixed:
        reasons.append(f"fixed ${amount:g} below floor {runtime.min_fixed}")
    if kind == "hourly" and min_hourly and amount is not None and amount < min_hourly:
        reasons.append(f"hourly ${amount:g}/hr below floor {min_hourly}")
    pay = (payment_status or "").lower()
    if runtime.require_verified_payment and pay not in {"verified", "true"}:
        reasons.append("requires verified payment")
    if runtime.min_client_rating is not None and rating is not None and rating < runtime.min_client_rating:
        reasons.append(f"client rating {rating} below {runtime.min_client_rating}")
    if runtime.min_client_hires is not None and hires is not None and hires < runtime.min_client_hires:
        reasons.append(f"client hires {hires} below {runtime.min_client_hires}")
    lowered = (blob or "").lower()
    for word in profile.exclude_keywords:
        if word.lower() in lowered:
            reasons.append(f"excluded keyword: {word}")
    for rule in rules:
        if rule.enforcement_level == "strict_block" and _rule_matches(rule.rule, lowered):
            reasons.append(f"strict_block: {rule.rule}")
    reasons.extend(hard_block_reasons(blob, runtime, contractor_type))
    reasons.extend(
        structured_skip_reasons(
            runtime,
            kind=kind,
            experience_level=experience_level,
            client_country=client_country,
            client_spend=client_spend,
            connects_cost=connects_cost,
            title=title,
            description=description,
            job_type=job_type or kind,
        )
    )
    return reasons


def settings_block_reasons_for_job(job: Job, session: Session, settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    runtime = load_runtime(session, settings)
    profile = load_profile(settings)
    rules = session.query(PreferenceRule).filter(PreferenceRule.active.is_(True)).all()
    kind, amount = parse_price_amount(job.job_type or "", job.price_label or job.budget or "", None)
    payment, rating, hires = _job_client_fields(job)
    attachment, contractor_type = _eligibility_fields(job)
    fields = job_filter_fields(_client_dict(job.client_json or job.client_info))
    return hard_gate_reasons(
        runtime,
        profile,
        rules,
        kind=kind,
        amount=amount,
        score=job.score,
        payment_status=payment,
        rating=rating,
        hires=hires,
        blob=f"{job.title}\n{job.description}\n{attachment}",
        contractor_type=contractor_type,
        experience_level=fields["experience_level"],
        client_country=fields["client_country"],
        client_spend=fields["client_spend"],
        connects_cost=fields["connects_cost"],
        title=job.title or "",
        description=job.description or "",
        job_type=job.job_type or "",
    )


def apply_runtime_filters(session: Session, settings: Settings | None = None) -> int:
    jobs = (
        session.query(Job)
        .filter(
            Job.status.in_([JobStatus.pending_review.value, JobStatus.submit_failed.value]),
            Job.applied_on_upwork.is_(False),
        )
        .all()
    )
    skipped = 0
    for job in jobs:
        reasons = settings_block_reasons_for_job(job, session, settings)
        if not reasons:
            continue
        job.status = JobStatus.skipped.value
        job.expires_at = None
        note = "skipped by settings: " + "; ".join(reasons)
        job.score_reason = note + (f"; {job.score_reason}" if job.score_reason else "")
        add_event(session, "skipped", note, job.id)
        skipped += 1
    if skipped:
        add_event(session, "settings", f"Re-applied scoring filters, skipped {skipped} pending jobs")
    return skipped


def _parse_client(payload: JobPayload) -> tuple[float | None, str, float | None, str]:
    rating = payload.get("client_rating")
    payment = payload.get("client_payment_status") or ""
    budget_value = payload.get("job_budget_value")
    duration = payload.get("estimated_duration") or ""
    raw = payload.get("client") or payload.get("raw") or ""
    try:
        data: Any = json.loads(raw) if raw.startswith("{") or raw.startswith("[") else {}
    except json.JSONDecodeError:
        data = {}
    if isinstance(data, dict):
        nested = data.get("client") if isinstance(data.get("client"), dict) else data
        if rating is None:
            for key in ("totalFeedback", "rating", "score", "feedback"):
                if key in nested and nested[key] not in (None, ""):
                    try:
                        rating = float(nested[key])
                    except (TypeError, ValueError):
                        pass
                    break
        if not payment:
            verified = nested.get("paymentVerified") or nested.get("isPaymentVerified")
            if verified is True:
                payment = "verified"
            elif verified is False:
                payment = "unverified"
        if budget_value is None:
            for key in ("amount", "hourlyRate", "budget"):
                val = nested.get(key) if isinstance(nested, dict) else None
                if isinstance(val, dict):
                    val = val.get("amount") or val.get("max")
                try:
                    if val is not None:
                        budget_value = float(val)
                except (TypeError, ValueError):
                    pass
    budget_text = payload.get("budget") or ""
    if budget_value is None:
        numbers = re.findall(r"(\d+(?:\.\d+)?)", budget_text.replace(",", ""))
        if numbers:
            try:
                budget_value = float(numbers[-1])
            except ValueError:
                pass
    return rating, str(payment), budget_value, duration


async def fetch_live_jobs(
    query_keywords: str,
    category_id: str = "",
    limit: int = 30,
    client: UpworkMcpClient | None = None,
) -> list[JobPayload]:
    args = FetchLiveJobsArgs(
        query_keywords=query_keywords,
        category_id=category_id,
        limit=limit,
    )
    mcp = client or UpworkMcpClient()
    pages = max(1, (max(1, args.limit) + 9) // 10)
    jobs = await mcp.search_jobs(args.query_keywords, max_pages=pages)
    return jobs[: max(1, args.limit)]


async def retrieve_matching_context(
    job_description_text: str,
    top_k_results: int = 6,
    db: Session | None = None,
) -> list[ContextMatch]:
    args = RetrieveContextArgs(
        job_description_text=job_description_text,
        top_k_results=top_k_results,
    )
    session, own = _session(db)
    try:
        matches = query_similar(session, args.job_description_text, top_k=args.top_k_results)
        return matches
    finally:
        if own:
            session.close()


def _learned_term_nudge(db: Session, text: str) -> tuple[int, str]:
    logs = db.query(FeedbackLog).order_by(FeedbackLog.created_at.desc()).limit(200).all()
    if len(logs) < 10:
        return 0, "learned weights cold-start"
    pos_tokens: dict[str, int] = {}
    neg_tokens: dict[str, int] = {}
    job_ids = {log.job_id for log in logs if log.job_id}
    jobs = {job.id: job for job in db.query(Job).filter(Job.id.in_(job_ids)).all()} if job_ids else {}
    positive = {"hired", "shortlisted", "messaged", "approved"}
    negative = {"rejected", "ignored"}
    for log in logs:
        job = jobs.get(log.job_id) if log.job_id else None
        blob = " ".join(
            [
                job.title if job else "",
                job.description if job else "",
                log.client_notes or "",
            ]
        )
        bucket = pos_tokens if log.outcome in positive else neg_tokens if log.outcome in negative else None
        if bucket is None:
            continue
        for tok in _tokens(blob):
            if len(tok) < 3:
                continue
            bucket[tok] = bucket.get(tok, 0) + 1
    job_tokens = _tokens(text)
    boost = 0
    hits: list[str] = []
    for tok in job_tokens:
        pos = pos_tokens.get(tok, 0)
        neg = neg_tokens.get(tok, 0)
        if pos + neg < 2:
            continue
        delta = min(8, pos - neg)
        if delta != 0:
            boost += delta
            hits.append(f"{tok}:{delta:+d}")
    boost = max(-20, min(20, boost))
    reason = "learned: " + ", ".join(hits[:6]) if hits else "learned: no overlapping labels"
    return boost, reason


async def execute_scoring_matrix(
    client_rating: float | None = None,
    client_payment_status: str = "",
    job_budget: float | None = None,
    estimated_duration: str = "",
    job_text: str = "",
    title: str = "",
    timezone: str = "",
    job_type: str = "",
    client_hires: int | None = None,
    proposal_count: int | None = None,
    hire_rate: float | None = None,
    invites_sent: int | None = None,
    interviewing: int | None = None,
    attachment_text: str = "",
    price_label: str = "",
    contractor_type: str = "",
    experience_level: str = "",
    client_country: str = "",
    client_spend: float | None = None,
    connects_cost: int | None = None,
    db: Session | None = None,
    settings: Settings | None = None,
) -> ScoreResult:
    args = ScoringMatrixArgs(
        client_rating=client_rating,
        client_payment_status=client_payment_status,
        job_budget=job_budget,
        estimated_duration=estimated_duration,
        job_text=job_text,
        title=title,
        timezone=timezone,
        job_type=job_type,
        client_hires=client_hires,
        proposal_count=proposal_count,
        hire_rate=hire_rate,
        invites_sent=invites_sent,
        interviewing=interviewing,
        attachment_text=attachment_text,
        price_label=price_label,
        contractor_type=contractor_type,
        experience_level=experience_level,
        client_country=client_country,
        client_spend=client_spend,
        connects_cost=connects_cost,
    )
    settings = settings or get_settings()
    session, own = _session(db)
    try:
        runtime = load_runtime(session, settings)
        matrix = load_scoring_config(settings)
        profile = load_profile(settings)
        blob = f"{args.title}\n{args.job_text}\n{args.attachment_text}".lower()
        breakdown: list[str] = []
        blocked = False
        score = matrix.base_score

        rules = session.query(PreferenceRule).filter(PreferenceRule.active.is_(True)).all()
        for rule in rules:
            if not _rule_matches(rule.rule, blob):
                continue
            if rule.enforcement_level == "strict_block":
                blocked = True
                breakdown.append(f"strict_block: {rule.rule}")
            else:
                score -= matrix.soft_penalty
                breakdown.append(f"soft_penalty: {rule.rule}")

        for word in profile.exclude_keywords:
            if word.lower() in blob:
                blocked = True
                breakdown.append(f"excluded keyword: {word}")

        for reason in hard_block_reasons(blob, runtime, args.contractor_type):
            blocked = True
            breakdown.append(reason)

        hits = [skill for skill in profile.skills if skill.lower() in blob]
        if hits:
            score += min(matrix.skill_bonus_cap, matrix.skill_bonus_per_hit * len(hits))
            breakdown.append("skills: " + ", ".join(hits))

        min_hourly = runtime.min_hourly if runtime.min_hourly is not None else profile.hourly_rate
        kind, amount = parse_price_amount(args.job_type, args.price_label, args.job_budget)
        if kind == "fixed" and runtime.min_fixed and amount is not None and amount < runtime.min_fixed:
            blocked = True
            breakdown.append(f"below fixed floor {runtime.min_fixed}")
        elif kind == "hourly" and min_hourly and amount is not None and amount < min_hourly:
            blocked = True
            breakdown.append(f"below rate floor {min_hourly}")

        for reason in structured_skip_reasons(
            runtime,
            kind=kind,
            experience_level=args.experience_level,
            client_country=args.client_country,
            client_spend=args.client_spend,
            connects_cost=args.connects_cost,
            title=args.title,
            description=args.job_text,
            job_type=args.job_type,
        ):
            blocked = True
            breakdown.append(reason)

        if runtime.require_verified_payment and args.client_payment_status.lower() not in {"verified", "true"}:
            blocked = True
            breakdown.append("requires verified payment")
        elif args.client_payment_status.lower() in {"unverified", "not verified", "false"}:
            score += matrix.payment_unverified
            breakdown.append("payment unverified")
        elif args.client_payment_status.lower() in {"verified", "true"}:
            score += matrix.payment_verified
            breakdown.append("payment verified")

        if args.client_rating is not None:
            if runtime.min_client_rating is not None and args.client_rating < runtime.min_client_rating:
                blocked = True
                breakdown.append(f"client rating below {runtime.min_client_rating}")
            elif args.client_rating < matrix.low_rating_below:
                score += matrix.low_rating
                breakdown.append(f"low client rating {args.client_rating}")
            elif args.client_rating >= matrix.strong_rating_at:
                score += matrix.strong_rating
                breakdown.append("strong client rating")

        if runtime.min_client_hires is not None and args.client_hires is not None and args.client_hires < runtime.min_client_hires:
            blocked = True
            breakdown.append(f"client hires {args.client_hires} below {runtime.min_client_hires}")

        if runtime.max_proposal_count is not None and args.proposal_count is not None and args.proposal_count > runtime.max_proposal_count:
            score += matrix.over_proposal_cap
            breakdown.append(f"{args.proposal_count} proposals already")

        if args.hire_rate is not None:
            if args.hire_rate >= matrix.hire_rate_high_at:
                score += matrix.hire_rate_high
                breakdown.append(f"hire rate {args.hire_rate:g}%")
            elif args.hire_rate < matrix.hire_rate_low_below and (args.client_hires or 0) >= matrix.hire_rate_low_min_hires:
                score += matrix.hire_rate_low
                breakdown.append(f"low hire rate {args.hire_rate:g}%")

        if args.interviewing is not None and args.interviewing >= matrix.interviewing_at:
            score += matrix.interviewing
            breakdown.append(f"{args.interviewing} already interviewing")
        elif args.invites_sent is not None and args.invites_sent >= matrix.invites_at:
            score += matrix.invites
            breakdown.append(f"{args.invites_sent} invites already sent")

        if args.attachment_text.strip():
            score += matrix.attachments
            breakdown.append("posting has attachments")

        preferred = [part.strip().lower() for part in runtime.prefer_timezones.split(",") if part.strip()]
        tz = args.timezone.lower()
        if preferred and tz and any(item in tz or tz in item for item in preferred):
            score += matrix.preferred_timezone
            breakdown.append(f"preferred timezone {args.timezone}")

        nudge, learned_reason = _learned_term_nudge(session, blob)
        score += nudge
        breakdown.append(learned_reason)

        matches = query_similar(session, blob, top_k=3, source_type="job")
        if matches:
            avg = sum(item.score for item in matches) / len(matches)
            if avg > matrix.similar_wins_above:
                score += matrix.similar_wins
                breakdown.append("similar to past wins")
            elif avg < matrix.unlike_wins_below:
                score += matrix.unlike_wins
                breakdown.append("unlike past wins")

        score = max(0, min(100, score))
        go = not blocked and score >= runtime.min_score
        reason = "; ".join(breakdown) if breakdown else "neutral"
        return ScoreResult(score=score, reason=reason, should_apply=go, go=go, breakdown=breakdown)
    finally:
        if own:
            session.close()


register_tool(
    ToolSpec(
        name="fetch_live_jobs",
        description="Query Upwork MCP for new job posts matching keywords.",
        parameters={
            "type": "object",
            "properties": {
                "query_keywords": {"type": "string"},
                "category_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query_keywords"],
        },
    ),
    fetch_live_jobs,
)

register_tool(
    ToolSpec(
        name="retrieve_matching_context",
        description="Semantic search over Upwork history and agent notes for a job description.",
        parameters={
            "type": "object",
            "properties": {
                "job_description_text": {"type": "string"},
                "top_k_results": {"type": "integer"},
            },
            "required": ["job_description_text"],
        },
    ),
    retrieve_matching_context,
)

register_tool(
    ToolSpec(
        name="execute_scoring_matrix",
        description="Rule-based Go/No-Go score using preferences, client metrics, and learned history.",
        parameters={
            "type": "object",
            "properties": {
                "client_rating": {"type": "number"},
                "client_payment_status": {"type": "string"},
                "job_budget": {"type": "number"},
                "estimated_duration": {"type": "string"},
                "job_text": {"type": "string"},
                "title": {"type": "string"},
            },
        },
    ),
    execute_scoring_matrix,
)
