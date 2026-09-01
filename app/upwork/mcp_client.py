from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import Settings, get_settings
from app.job_attachments import FILE_EXTS, store_job_attachments
from app.milestones import mcp_milestone_params
from app.models import ApplyHighlight, ConnectsPanel, JobPayload, McpStatus, ProposalMilestone, ScreeningAnswer, ToolCallArgs
from app.proposal_writer import clean_screening_question
from app.upwork.oauth import build_oauth_provider
from app.upwork.token_store import token_storage

logger = logging.getLogger(__name__)


def format_mcp_error(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()

    def is_wrapper(err: BaseException) -> bool:
        name = type(err).__name__
        if name in {"ExceptionGroup", "BaseExceptionGroup", "TaskGroupError"}:
            return True
        text = str(err).strip().lower()
        return "unhandled errors in a taskgroup" in text

    def walk(err: BaseException) -> None:
        if id(err) in seen:
            return
        seen.add(id(err))
        nested = getattr(err, "exceptions", None)
        if nested:
            for item in nested:
                if isinstance(item, BaseException):
                    walk(item)
            if is_wrapper(err):
                return
        text = str(err).strip()
        if is_wrapper(err) or not text:
            cause = err.__cause__ or err.__context__
            if isinstance(cause, BaseException):
                walk(cause)
            return
        if text not in parts:
            parts.append(text)

    walk(exc)
    return "; ".join(parts) or type(exc).__name__


def oauth_needs_login(message: str) -> bool:
    lower = (message or "").lower()
    return "interactive oauth" in lower or "connect from the dashboard" in lower


def _sanitize_list_tools_raw(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    if raw.get("cacheScope") == "":
        raw["cacheScope"] = "private"
    return raw


def _absorb_listing(session: Any, listed: Any) -> Any:
    absorb = getattr(session, "_absorb_tool_listing", None)
    if absorb is None:
        return listed
    return absorb(listed, complete=True)


async def list_session_tools(session: Any) -> list[Any]:
    from mcp.types import ListToolsResult

    try:
        listed = await session.list_tools()
        return list(listed.tools)
    except Exception:
        pass
    dispatcher = getattr(session, "_dispatcher", None)
    if dispatcher is None:
        return []
    raw = _sanitize_list_tools_raw(await dispatcher.send_raw_request("tools/list", None, {}))
    if not raw:
        return []
    listed = _absorb_listing(session, ListToolsResult.model_validate(raw))
    return list(listed.tools)


async def call_session_tool(session: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    arguments = arguments or {}
    dispatcher = getattr(session, "_dispatcher", None)
    if dispatcher is None:
        return await session.call_tool(name, arguments)
    raw = await dispatcher.send_raw_request("tools/call", {"name": name, "arguments": arguments}, {})
    if isinstance(raw, dict) and raw.get("isError"):
        raise RuntimeError(_tool_text(raw) or f"{name} failed")
    return raw


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    return {}


def public_job_url(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    match = re.search(r"(~0[12]\d+)", text)
    if match:
        return f"https://www.upwork.com/jobs/{match.group(1)}"
    digits = re.search(r"(?:/jobs/)?(\d{10,})", text)
    if digits:
        return f"https://www.upwork.com/jobs/~02{digits.group(1)}"
    if text.startswith("http"):
        return text
    return f"https://www.upwork.com/jobs/{text}"


def job_ref_keys(value: str | None) -> set[str]:
    text = (value or "").strip()
    keys = {text} if text else set()
    if text.startswith("~0") and len(text) > 3 and text[3:].isdigit():
        keys.add(text[3:])
    elif text.isdigit():
        keys.add(f"~02{text}")
        keys.add(f"~01{text}")
    return keys


def already_applied(message: str) -> bool:
    lower = (message or "").lower()
    return (
        "vj-ja-10" in lower
        or "already applied" in lower
        or "already have a proposal" in lower
        or "existing proposal" in lower
        or "prior proposal" in lower
        or "invitation exists" in lower
    )


def _skip_attachment_confirm(message: str) -> bool:
    lower = (message or "").lower()
    return (
        "task_id is required" in lower
        or "do not need confirming" in lower
        or "already stored" in lower
        or "inline component" in lower
    )


def _tool_text(result: Any) -> str:
    if isinstance(result, dict):
        structured = result.get("structuredContent") or result.get("structured_content")
        if structured is not None:
            if isinstance(structured, (dict, list)):
                return json.dumps(structured)
            return str(structured)
        content = result.get("content") or []
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "\n".join(parts)
        if result.get("isError") or result.get("error"):
            return str(result.get("error") or result)
        return json.dumps(result, default=str)
    parts: list[str] = []
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured is not None:
        if isinstance(structured, (dict, list)):
            return json.dumps(structured)
        return str(structured)
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
        elif isinstance(item, dict) and item.get("text"):
            parts.append(str(item["text"]))
    return "\n".join(parts)


def _parse_jsonish(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                pass
        start_l = stripped.find("[")
        end_l = stripped.rfind("]")
        if start_l >= 0 and end_l > start_l:
            try:
                return json.loads(stripped[start_l : end_l + 1])
            except json.JSONDecodeError:
                pass
        return {"text": stripped}


def _walk_jobs(value: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(value, list):
        for item in value:
            _walk_jobs(item, found)
        return
    if not isinstance(value, dict):
        return
    keys = {k.lower() for k in value.keys()}
    looks_like_job = bool({"id", "job_id", "ciphertext", "uid"} & keys) and (
        "title" in keys or "description" in keys or "job" in keys
    )
    if looks_like_job:
        found.append(value)
        return
    for nested_key in ("jobs", "items", "results", "nodes", "data", "marketplaceJobPostings"):
        if nested_key in value:
            _walk_jobs(value[nested_key], found)
            return
    for nested in value.values():
        if isinstance(nested, (dict, list)):
            _walk_jobs(nested, found)


_UNTRUSTED = re.compile(r"</?untrusted_participant_content>\s*", re.IGNORECASE)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return _UNTRUSTED.sub("", str(value)).strip()


def _pick(data: dict[str, Any], *names: str) -> Any:
    lower = {k.lower(): v for k, v in data.items()}
    for name in names:
        if name.lower() in lower and lower[name.lower()] not in (None, ""):
            return lower[name.lower()]
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return _number(value.get("rawValue") or value.get("amount") or value.get("displayValue"))
    try:
        return float(str(value).replace("$", "").replace(",", "").split("-")[0].strip())
    except (TypeError, ValueError):
        return None


_GENERIC_PRICES = {"", "Fixed", "Hourly", "Hourly · range set", "Hourly · no range", "—"}


def prefer_price_label(*labels: str | None) -> str:
    texts = [str(item).strip() for item in labels if item and str(item).strip()]
    for text in texts:
        if "$" in text:
            return text
    for text in reversed(texts):
        if text not in _GENERIC_PRICES:
            return text
    return next((text for text in reversed(texts) if text), "")


def _price_label(job_type: str, hourly_min: float | None, hourly_max: float | None, budget: float | None, hourly_kind: str) -> str:
    kind = hourly_kind.lower()
    if job_type == "hourly":
        if hourly_min is not None and hourly_max is not None:
            if hourly_min == hourly_max:
                return f"${hourly_min:g}/hr"
            return f"${hourly_min:g}–${hourly_max:g}/hr"
        if hourly_min is not None:
            return f"${hourly_min:g}+/hr"
        if "not_provided" in kind or "no rate" in kind:
            return "Hourly · no range"
        if hourly_kind:
            return "Hourly · range set"
        return "Hourly"
    if budget is not None and budget > 0:
        return f"${budget:g} fixed"
    return "Fixed"


def price_from_raw(raw: dict[str, Any]) -> str:
    blob = _client_blob(raw)
    job_type = str(blob.get("job_type") or "")
    hourly_min = blob.get("hourly_min") if isinstance(blob.get("hourly_min"), (int, float)) else None
    hourly_max = blob.get("hourly_max") if isinstance(blob.get("hourly_max"), (int, float)) else None
    budget = blob.get("budget") if isinstance(blob.get("budget"), (int, float)) else None
    return _price_label(job_type, hourly_min, hourly_max, budget, str(blob.get("hourly_kind") or ""))


def merge_client_details(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if value in (None, "", [], {}):
            continue
        if key == "verified" and merged.get("verified") is True and value is False:
            continue
        merged[key] = value
    return merged


def _coalesce_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _int_value(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _first_date(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text.split("T", 1)[0][:10]
    return ""


def _iso_datetime(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        normalized = text.replace("Z", "+00:00")
        if re.search(r"[+-]\d{4}$", normalized):
            normalized = normalized[:-2] + ":" + normalized[-2:]
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    return ""


def _member_since(*blobs: Any) -> str:
    names = (
        "member_since",
        "memberSince",
        "joined",
        "joined_date",
        "joinedDate",
        "registrationDate",
        "registration_date",
        "createdOn",
        "created_on",
    )
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for name in names:
            found = _first_date(blob.get(name))
            if found:
                return found
        nested = blob.get("stats") if isinstance(blob.get("stats"), dict) else {}
        for name in names:
            found = _first_date(nested.get(name))
            if found:
                return found
    return ""


def _job_activity(activity: dict[str, Any]) -> dict[str, int | None]:
    job_act = activity.get("jobActivity") if isinstance(activity.get("jobActivity"), dict) else activity
    return {
        "invites_sent": _int_value(job_act.get("invitesSent") or job_act.get("invites_sent")),
        "interviewing": _int_value(
            job_act.get("totalInvitedToInterview") or job_act.get("interviewing") or job_act.get("total_invited_to_interview")
        ),
        "hired_on_job": _int_value(job_act.get("totalHired") or job_act.get("total_hired")),
        "unanswered_invites": _int_value(job_act.get("totalUnansweredInvites") or job_act.get("unanswered_invites")),
        "offered_on_job": _int_value(job_act.get("totalOffered") or job_act.get("total_offered")),
    }


def _client_reviews(history: dict[str, Any]) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for bucket in ("closed", "open"):
        rows = history.get(bucket)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            comment = _clean_text(
                row.get("feedback_to_client_comment")
                or row.get("feedbackComment")
                or row.get("comment")
                or row.get("feedback_comment")
                or ""
            )
            to_client = _number(row.get("feedback_to_client_score") or row.get("feedbackToClientScore"))
            reviewer = _clean_text(
                row.get("freelancer_name")
                or row.get("contractor_name")
                or row.get("freelancer")
                or row.get("reviewer")
                or ""
            )
            title = _clean_text(row.get("title") or "")
            if to_client is None and not comment and not reviewer:
                if bucket != "closed" or _number(row.get("feedback_score")) is None:
                    continue
            reviews.append(
                {
                    "title": title,
                    "reviewer": reviewer,
                    "rating": to_client if to_client is not None else _number(row.get("feedback_score")),
                    "comment": comment,
                }
            )
    return reviews


_FILE_EXTS = FILE_EXTS


def _looks_like_file(node: dict[str, Any], url: str, name: str) -> bool:
    if not url.startswith("http"):
        return False
    keys = {str(key).lower() for key in node}
    if keys & {"filename", "file_name", "fileuid", "file_uid", "fileurl", "downloadurl", "download_url"}:
        return True
    lower_name = name.lower()
    lower_url = url.split("?", 1)[0].lower()
    return any(lower_name.endswith(ext) or lower_url.endswith(ext) for ext in _FILE_EXTS)


def _attachment_refs(raw: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def walk(node: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        url = str(
            node.get("url")
            or node.get("downloadUrl")
            or node.get("download_url")
            or node.get("fileUrl")
            or node.get("file_url")
            or ""
        )
        name = str(
            node.get("fileName")
            or node.get("file_name")
            or node.get("filename")
            or node.get("name")
            or node.get("title")
            or ""
        )
        if _looks_like_file(node, url, name):
            filename = name or url.rsplit("/", 1)[-1].split("?", 1)[0] or "attachment"
            key = (url, filename)
            if key not in seen:
                seen.add(key)
                found.append({"filename": filename, "url": url})
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, depth + 1)

    walk(raw, 0)
    return found


def derive_client_stats(blob: dict[str, Any]) -> None:
    hires = _number(blob.get("hires"))
    posted = _number(blob.get("posted_jobs"))
    spend = _number(blob.get("spend_total"))
    hours = _number(blob.get("hours_total"))
    if hires is not None and posted and posted > 0:
        computed = min(100, round(100.0 * float(hires) / float(posted)))
        existing = blob.get("hire_rate")
        if existing in (None, "") or (isinstance(existing, (int, float)) and (existing > 100 or existing < 0)):
            blob["hire_rate"] = computed
    if blob.get("avg_spend") in (None, "") and spend is not None and hires and hires > 0:
        blob["avg_spend"] = round(float(spend) / float(hires), 2)
    if blob.get("avg_hourly_paid") in (None, "") and spend is not None and hours and hours > 0:
        blob["avg_hourly_paid"] = round(float(spend) / float(hours), 2)


def fold_search_client(details: dict[str, Any], search: dict[str, Any]) -> dict[str, Any]:
    if not search:
        return details
    mapping = {
        "posted_jobs": search.get("total_posted_jobs") or search.get("posted_jobs"),
        "reviews": search.get("total_reviews") or search.get("reviews"),
        "hires": search.get("total_hires") or search.get("hires"),
        "rating": search.get("rating"),
        "country": search.get("country"),
        "city": search.get("city"),
        "state": search.get("state"),
        "timezone": search.get("timezone"),
        "hire_rate": search.get("hire_rate") or search.get("hireRate"),
    }
    for key, value in mapping.items():
        if details.get(key) in (None, "", []) and value not in (None, ""):
            details[key] = value
    status = search.get("verification_status")
    if status in (True, "VERIFIED", "verified"):
        details["verified"] = True
    elif details.get("verified") is not True and status in (False, "NOT_VERIFIED", "unverified"):
        details["verified"] = False
    derive_client_stats(details)
    return details


def _client_blob(raw: dict[str, Any]) -> dict[str, Any]:
    posting = raw
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    nested = data.get("marketplaceJobPosting") if isinstance(data.get("marketplaceJobPosting"), dict) else None
    if nested is None and isinstance(raw.get("marketplaceJobPosting"), dict):
        nested = raw["marketplaceJobPosting"]
    if isinstance(nested, dict):
        posting = nested
    content = posting.get("content") if isinstance(posting.get("content"), dict) else {}
    company = posting.get("clientCompanyPublic") if isinstance(posting.get("clientCompanyPublic"), dict) else {}
    terms = posting.get("contractTerms") if isinstance(posting.get("contractTerms"), dict) else {}
    hourly_terms = terms.get("hourlyContractTerms") if isinstance(terms.get("hourlyContractTerms"), dict) else {}
    fixed_terms = terms.get("fixedPriceContractTerms") if isinstance(terms.get("fixedPriceContractTerms"), dict) else {}
    activity = posting.get("activityStat") if isinstance(posting.get("activityStat"), dict) else {}
    bids = activity.get("applicationsBidStats") if isinstance(activity.get("applicationsBidStats"), dict) else {}
    search_client = raw.get("client") if isinstance(raw.get("client"), dict) else {}
    record = raw.get("client_record") if isinstance(raw.get("client_record"), dict) else {}
    history = raw.get("client_work_history") if isinstance(raw.get("client_work_history"), dict) else {}
    quals = raw.get("preferred_qualifications") if isinstance(raw.get("preferred_qualifications"), dict) else {}
    country = company.get("country")
    country_name = country.get("name") if isinstance(country, dict) else country
    country_name = country_name or search_client.get("country")
    rating = record.get("feedback_score") or search_client.get("rating")
    status = search_client.get("verification_status")
    verified: bool | None
    if status in (True, "VERIFIED", "verified"):
        verified = True
    elif status in (False, "NOT_VERIFIED", "unverified"):
        verified = False
    else:
        verified = None
    job_type = str(raw.get("job_type") or terms.get("contractType") or posting.get("job_type") or "").lower()
    if job_type == "hourlycontractterms" or str(terms.get("contractType") or "").upper() == "HOURLY":
        job_type = "hourly"
    if str(terms.get("contractType") or "").upper() in {"FIXED", "FIXED_PRICE"} or job_type in {"fixed", "fixed_price"}:
        job_type = "fixed"
    hourly_min = (
        _number(hourly_terms.get("hourlyBudgetMin"))
        or _number(raw.get("hourly_budget_min"))
        or _number(posting.get("hourly_budget_min"))
    )
    hourly_max = (
        _number(hourly_terms.get("hourlyBudgetMax"))
        or _number(raw.get("hourly_budget_max"))
        or _number(posting.get("hourly_budget_max"))
    )
    budget = (
        _number(fixed_terms.get("amount"))
        or _number(raw.get("budget"))
        or _number(posting.get("budget"))
    )
    hourly_kind = str(raw.get("hourly_budget_type") or posting.get("hourly_budget_type") or hourly_terms.get("hourlyBudgetType") or "")
    timezone = str(company.get("timezone") or search_client.get("timezone") or "")
    location_city = company.get("city") or search_client.get("city")
    location_state = company.get("state") or search_client.get("state")
    proposal_count = raw.get("proposal_count")
    if proposal_count is None:
        proposal_count = (activity.get("jobActivity") or {}).get("applicationsCount") if isinstance(activity.get("jobActivity"), dict) else None
    hire_rate = _number(
        record.get("hire_rate")
        or record.get("hireRate")
        or company.get("hireRate")
        or search_client.get("hire_rate")
        or search_client.get("hireRate")
    )
    avg_hourly = _number(
        record.get("avg_hourly_paid")
        or record.get("avgHourlyPaid")
        or record.get("averageHourlyRate")
        or record.get("avg_hourly_rate")
    )
    blob: dict[str, Any] = {
        "id": str(posting.get("id") or raw.get("id") or ""),
        "title": _clean_text(content.get("title") or raw.get("title") or ""),
        "description": _clean_text(
            content.get("description") or raw.get("description") or raw.get("description_snippet") or ""
        ),
        "job_type": job_type or ("hourly" if hourly_min is not None else ""),
        "hourly_min": hourly_min,
        "hourly_max": hourly_max,
        "budget": budget,
        "hourly_kind": hourly_kind,
        "timezone": timezone,
        "city": location_city,
        "state": location_state,
        "country": country_name,
        "company": company.get("name") or search_client.get("name") or search_client.get("company_name"),
        "rating": _number(rating),
        "reviews": record.get("feedback_count") or search_client.get("total_reviews"),
        "hires": _coalesce_number(
            record.get("jobs_with_hires"),
            search_client.get("total_hires"),
            record.get("contracts_total"),
        ),
        "posted_jobs": _coalesce_number(
            record.get("jobs_posted"),
            record.get("posted_jobs"),
            record.get("jobsPosted"),
            search_client.get("total_posted_jobs"),
        ),
        "verified": verified,
        "spend_total": record.get("spend_total"),
        "hours_total": record.get("hours_total"),
        "contracts_active": record.get("contracts_active"),
        "contracts_total": record.get("contracts_total"),
        "proposal_count": proposal_count or raw.get("proposal_count"),
        "avg_bid": _number((bids.get("avgRateBid") or {}).get("rawValue") if isinstance(bids.get("avgRateBid"), dict) else bids.get("avgRateBid")),
        "experience_level": raw.get("experience_level") or terms.get("experienceLevel"),
        "duration": raw.get("duration"),
        "engagement": raw.get("engagement") or hourly_terms.get("engagementType"),
        "preferred_qualifications": quals,
        "open_contracts": history.get("open") if isinstance(history.get("open"), list) else [],
        "closed_contracts": history.get("closed") if isinstance(history.get("closed"), list) else [],
        "connects_cost": raw.get("connects_cost"),
        "can_apply": raw.get("can_apply"),
        "search_client": search_client,
    }
    posted = _iso_datetime(
        raw.get("published_date"),
        raw.get("publishedDate"),
        posting.get("publishedDateTime"),
        posting.get("publishedOn"),
        content.get("publishedDateTime"),
        raw.get("created_date"),
        raw.get("createdDate"),
        posting.get("createdDateTime"),
        content.get("createdDateTime"),
    )
    if posted:
        blob["published_date"] = posted
    if hire_rate is not None:
        blob["hire_rate"] = hire_rate
    if avg_hourly is not None:
        blob["avg_hourly_paid"] = avg_hourly
    for key, value in _job_activity(activity if isinstance(activity, dict) else {}).items():
        if value is not None:
            blob[key] = value
    member = _member_since(company, record, search_client, raw)
    if member:
        blob["member_since"] = member
    reviews = _client_reviews(history if isinstance(history, dict) else {})
    if reviews:
        blob["client_reviews"] = reviews
    attachments = _attachment_refs(raw)
    if attachments:
        blob["attachments"] = attachments
    derive_client_stats(blob)
    return blob


def normalize_job(raw: dict[str, Any]) -> JobPayload | None:
    blob = _client_blob(raw)
    job_id = blob.get("id") or _pick(raw, "id", "job_id", "jobId", "ciphertext", "uid")
    title = blob.get("title") or _clean_text(_pick(raw, "title", "jobTitle") or "")
    if not job_id or not title:
        return None
    posting = raw.get("data", {}).get("marketplaceJobPosting") if isinstance(raw.get("data"), dict) else {}
    if not isinstance(posting, dict):
        posting = raw.get("marketplaceJobPosting") if isinstance(raw.get("marketplaceJobPosting"), dict) else {}
    cipher = str(_pick(raw, "ciphertext") or posting.get("ciphertext") or "")
    link = public_job_url(cipher or str(job_id))
    job_type = str(blob.get("job_type") or "")
    hourly_min = blob.get("hourly_min") if isinstance(blob.get("hourly_min"), (int, float)) else None
    hourly_max = blob.get("hourly_max") if isinstance(blob.get("hourly_max"), (int, float)) else None
    budget_num = blob.get("budget") if isinstance(blob.get("budget"), (int, float)) else None
    price = _price_label(job_type, hourly_min, hourly_max, budget_num, str(blob.get("hourly_kind") or ""))
    budget_value = hourly_min if job_type == "hourly" and hourly_min else budget_num
    client_details = {
        key: value
        for key, value in blob.items()
        if key not in {"id", "title", "description", "search_client"} and value not in (None, "", [], {})
    }
    fold_search_client(client_details, blob.get("search_client") if isinstance(blob.get("search_client"), dict) else {})
    payload: JobPayload = {
        "id": str(job_id),
        "title": str(title),
        "description": str(blob.get("description") or ""),
        "raw": json.dumps(raw, default=str),
        "url": link,
        "budget": price,
        "price_label": price,
        "timezone": str(blob.get("timezone") or ""),
        "job_type": job_type,
        "client": json.dumps(blob.get("search_client") or {}, default=str),
        "client_details": json.dumps(client_details, default=str),
        "estimated_duration": str(blob.get("duration") or blob.get("engagement") or ""),
    }
    if budget_value is not None:
        payload["job_budget_value"] = float(budget_value)
    rating = blob.get("rating")
    if isinstance(rating, (int, float)):
        payload["client_rating"] = float(rating)
    if blob.get("verified") is True:
        payload["client_payment_status"] = "verified"
    elif blob.get("verified") is False:
        payload["client_payment_status"] = "unverified"
    hires = blob.get("hires")
    if isinstance(hires, (int, float)):
        payload["client_hires"] = int(hires)
    proposals = blob.get("proposal_count")
    if isinstance(proposals, (int, float)):
        payload["proposal_count"] = int(proposals)
    hire_rate = blob.get("hire_rate")
    if isinstance(hire_rate, (int, float)):
        payload["hire_rate"] = float(hire_rate)
    invites_sent = blob.get("invites_sent")
    if isinstance(invites_sent, (int, float)):
        payload["invites_sent"] = int(invites_sent)
    interviewing = blob.get("interviewing")
    if isinstance(interviewing, (int, float)):
        payload["interviewing"] = int(interviewing)
    return payload


def jobs_from_tool_result(result: Any) -> list[JobPayload]:
    parsed = _parse_jsonish(_tool_text(result))
    jobs: list[JobPayload] = []
    seen: set[str] = set()

    def add(raw: dict[str, Any]) -> None:
        job = normalize_job(raw)
        if job is None or job["id"] in seen:
            return
        seen.add(job["id"])
        jobs.append(job)

    if isinstance(parsed, dict):
        if isinstance(parsed.get("jobs"), list):
            for item in parsed["jobs"]:
                if isinstance(item, dict):
                    add(item)
            return jobs
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        if isinstance(data.get("marketplaceJobPosting"), dict) or isinstance(parsed.get("marketplaceJobPosting"), dict):
            add(parsed)
            return jobs
        if parsed.get("id") and (parsed.get("title") or parsed.get("description") or parsed.get("description_snippet")):
            add(parsed)
            return jobs
    found: list[dict[str, Any]] = []
    _walk_jobs(parsed, found)
    for raw in found:
        add(raw)
    return jobs


def _tool_name_score(name: str, kinds: tuple[str, ...]) -> int:
    lowered = name.lower().replace("-", "_")
    score = 0
    for kind in kinds:
        if kind in lowered:
            score += 2
    return score


def extract_org_uid(value: Any) -> str:
    talent: list[str] = []
    other: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        uid = node.get("org_uid") or node.get("orgUid") or node.get("organization_uid")
        role = str(node.get("role") or "").upper()
        if uid:
            if role in {"TALENT", "FREELANCER", "INDEPENDENT"}:
                talent.append(str(uid))
            else:
                other.append(str(uid))
        for item in node.values():
            if isinstance(item, (dict, list)):
                walk(item)

    walk(value)
    if talent:
        return talent[0]
    return other[0] if other else ""


def page_has_more(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if lowered in {"hasmore", "has_more", "hasnextpage"} and item is True:
                return True
            if lowered in {"pagenext", "next_cursor", "endcursor"} and item:
                return True
            if isinstance(item, (dict, list)) and page_has_more(item):
                return True
        return False
    if isinstance(value, list):
        return any(page_has_more(item) for item in value)
    return False


PROPOSAL_MEMORY_STATUSES: tuple[str, ...] = (
    "Pending",
    "Accepted",
    "Submitted",
    "Active",
    "Offered",
    "Hired",
    "Activated",
)
PROPOSAL_CLOSED_STATUSES: tuple[str, ...] = ("Declined", "Withdrawn", "Archived")
POLL_PROPOSAL_STATUSES: tuple[str, ...] = (
    "Offered",
    "Hired",
    "Declined",
    "Archived",
    "Activated",
    "Accepted",
)


async def paginate_freelancer_proposals(
    named: Any,
    statuses: tuple[str, ...],
    *,
    max_pages: int,
) -> list[Any]:
    pages: list[Any] = []
    for status in statuses:
        cursor = ""
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "limit": 10,
                "status": status,
                "sort_field": "CREATEDDATETIME",
                "sort_order": "DESC",
            }
            if cursor:
                params["cursor"] = cursor
            page = await named("upwork__list_freelancer_proposals", "list", params)
            pages.append(page)
            if isinstance(page, dict) and page.get("error"):
                break
            nxt = next_cursor(page)
            if not nxt or nxt == cursor:
                break
            cursor = nxt
    return pages


def next_cursor(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower().replace("-", "_")
            if lowered in {"cursor", "next_cursor", "endcursor", "after"} and item and isinstance(item, (str, int)):
                return str(item)
            nested = next_cursor(item) if isinstance(item, (dict, list)) else ""
            if nested:
                return nested
        return ""
    if isinstance(value, list):
        for item in value:
            nested = next_cursor(item)
            if nested:
                return nested
    return ""


def pick_tool(tools: list[Any], *needles: str) -> Any | None:
    ranked: list[tuple[int, Any]] = []
    for tool in tools:
        name = getattr(tool, "name", "") or ""
        score = _tool_name_score(name, needles)
        if score:
            ranked.append((score, tool))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _schema_props(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump()
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") or {}
    return props if isinstance(props, dict) else {}


def build_search_args(tool: Any, query: str) -> ToolCallArgs:
    props = _schema_props(tool)
    args: ToolCallArgs = {}
    for key in ("query", "keyword", "search", "q", "keywords", "searchQuery"):
        if key in props:
            args[key] = query  # type: ignore[literal-required]
            return args
    if not props:
        args["query"] = query
        return args
    first = next(iter(props.keys()))
    return {first: query}  # type: ignore[return-value]


def build_job_id_args(tool: Any, job_id: str) -> dict[str, Any]:
    props = _schema_props(tool)
    for key in ("job_id", "jobId", "id", "ciphertext", "uid"):
        if key in props:
            return {key: job_id}
    if not props:
        return {"job_id": job_id}
    first = next(iter(props.keys()))
    return {first: job_id}


def build_submit_args(tool: Any, job_id: str, cover_letter: str) -> dict[str, Any]:
    props = _schema_props(tool)
    args: dict[str, Any] = {}
    id_key = next((k for k in ("job_id", "jobId", "id", "ciphertext") if k in props), None)
    letter_key = next((k for k in ("cover_letter", "coverLetter", "proposal", "letter") if k in props), None)
    if id_key:
        args[id_key] = job_id
    if letter_key:
        args[letter_key] = cover_letter
    if args:
        remaining = [k for k in props if k not in args]
        for key in remaining:
            nested = props.get(key)
            if isinstance(nested, dict) and nested.get("type") == "object":
                nested_props = nested.get("properties") or {}
                inner: dict[str, Any] = {}
                if "cover_letter" in nested_props or "coverLetter" in nested_props:
                    inner_key = "cover_letter" if "cover_letter" in nested_props else "coverLetter"
                    inner[inner_key] = cover_letter
                if inner:
                    args[key] = inner
        return args
    return {"job_id": job_id, "cover_letter": cover_letter}


def _as_int(value: Any) -> int | None:
    if value is None or value is False or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def parse_connects_balance(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, dict):
        return None
    direct = value.get("connectsBalance")
    if isinstance(direct, (int, float)):
        return int(direct)
    nested = value.get("balance")
    if isinstance(nested, dict):
        found = parse_connects_balance(nested)
        if found is not None:
            return found
    if isinstance(nested, (int, float)):
        return int(nested)
    for key in ("connects_balance", "connects"):
        found = parse_connects_balance(value.get(key))
        if found is not None:
            return found
    data = value.get("data")
    if isinstance(data, dict):
        return parse_connects_balance(data)
    return None


def connects_required_from_text(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"costs?\s+(\d+)\s+Connects", text, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"need\s+(\d+)", text, re.I)
    if match and "connect" in text.lower():
        return int(match.group(1))
    return None


def connects_shortage(text: str) -> bool:
    lower = (text or "").lower()
    return "enough connects" in lower or "don't have enough" in lower


def parse_proposal_preview(raw: Any) -> ConnectsPanel:
    payload = _as_dict(raw)
    preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else payload
    boost = preview.get("boost") if isinstance(preview.get("boost"), dict) else {}
    available = _as_int(preview.get("connects_balance"))
    if available is None:
        available = _as_int(boost.get("your_balance"))
    apply_cost = _as_int(preview.get("connects_cost"))
    raw_bids = boost.get("current_top_bids")
    top_bids = [_as_int(item) for item in raw_bids] if isinstance(raw_bids, list) else []
    bids = [item for item in top_bids if item is not None]
    boost_available = bool(boost) and boost.get("available") is not False and boost.get("recommendation") != "skip"
    error = ""
    for key in ("error", "message", "detail"):
        found = payload.get(key) or preview.get(key)
        if found:
            error = str(found).strip()
            break
    blob = json.dumps(payload, default=str)
    if isinstance(raw, str) and not payload:
        error = raw[:2000]
        blob = raw
    if not error and connects_shortage(blob):
        error = blob[:2000]
    required = connects_required_from_text(error or blob)
    if required is not None:
        apply_cost = required
    remaining = None
    if available is not None and apply_cost is not None:
        remaining = available - apply_cost
    charged = preview.get("charged_amount")
    charged_amount = float(charged) if isinstance(charged, (int, float)) else None
    can_apply = preview.get("can_apply")
    if connects_shortage(error) or (remaining is not None and remaining < 0):
        can_apply = False
    return ConnectsPanel(
        available=available,
        apply_cost=apply_cost,
        can_apply=can_apply if isinstance(can_apply, bool) else None,
        boost_available=boost_available,
        boost_reason=str(boost.get("reason") or boost.get("note") or ""),
        recommended_connects=_as_int(boost.get("recommended_connects")),
        top_bids=bids,
        bid_count=len(bids),
        bids_unknown=boost.get("current_top_bids_available") is False,
        rationale=str(boost.get("rationale") or ""),
        error=error if isinstance(error, str) else "",
        charged_amount=charged_amount,
        remaining_after_apply=remaining,
        milestones_allowed=preview.get("milestones_allowed") is True,
        screening_questions=parse_screening_questions(payload),
    )


def parse_screening_questions(raw: Any) -> list[str]:
    payload = _as_dict(raw)
    blobs: list[Any] = [payload, payload.get("preview"), payload.get("data")]
    preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
    blobs.append(preview.get("job") if isinstance(preview, dict) else None)
    questions: list[str] = []
    for blob in blobs:
        data = _as_dict(blob)
        rows = data.get("screening_questions")
        if not isinstance(rows, list):
            continue
        for item in rows:
            if isinstance(item, str):
                text = clean_screening_question(item)
            elif isinstance(item, dict):
                text = clean_screening_question(str(item.get("question") or item.get("text") or item.get("prompt") or ""))
            else:
                text = ""
            if text and text not in questions:
                questions.append(text)
    return questions


def parse_highlights(raw: Any) -> list[ApplyHighlight]:
    payload = _as_dict(raw)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items: list[ApplyHighlight] = []
    buckets = (
        (data.get("portfolio_projects") or payload.get("portfolio_projects"), "portfolio"),
        (data.get("certificates") or payload.get("certificates"), "certificate"),
    )
    for rows, kind in buckets:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            hid = str(row.get("id") or row.get("uid") or "")
            title = str(row.get("title") or row.get("name") or hid)
            if hid:
                items.append(ApplyHighlight(kind=kind, id=hid, title=title))
    return items


def _first_str(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _file_uids(raw: Any) -> list[str]:
    payload = raw
    if isinstance(raw, str):
        payload = _parse_jsonish(raw)
    found: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            found.extend(_file_uids(item))
        return list(dict.fromkeys(found))
    data = _as_dict(payload)
    if not data:
        return []
    direct = data.get("file_uid") or data.get("file_id")
    if isinstance(direct, str) and direct:
        found.append(direct)
    for key in ("file_uids", "file_ids"):
        value = data.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item:
                    found.append(item)
    for key in ("files", "attachments", "data", "preview"):
        if key in data:
            found.extend(_file_uids(data.get(key)))
    return list(dict.fromkeys(found))


def _write_draft(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload
    nested = payload.get("preview") if isinstance(payload.get("preview"), dict) else None
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else None
    for blob in (payload, nested, inner):
        item = _as_dict(blob)
        draft_id = _first_str(item, "draft_id", "draftId")
        kind = _first_str(item, "type", "draft_type", "entity_type")
        if draft_id:
            return draft_id, kind
    return "", ""


def _looks_sent(payload: dict[str, Any], text: str) -> bool:
    status = str(payload.get("status") or "").lower()
    if status in {"ok", "success", "sent"} and payload.get("draft_id"):
        return False
    if status == "sent":
        return True
    story = str(payload.get("story_id") or payload.get("id") or "")
    if story.startswith("story_"):
        return True
    return False


def _rooms_page(payload: Any) -> tuple[list[dict[str, Any]], str, bool]:
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]
    blob = _as_dict(data)
    raw = blob.get("rooms")
    if not isinstance(raw, list):
        raw = blob.get("items") if isinstance(blob.get("items"), list) else []
    rooms = [item for item in raw if isinstance(item, dict)]
    cursor = ""
    more = False
    if isinstance(payload, dict):
        cursor = str(payload.get("next_cursor") or payload.get("cursor") or "")
        more = bool(payload.get("hasMore") or payload.get("has_more"))
    if not cursor:
        cursor = str(blob.get("next_cursor") or "")
    if not more:
        more = bool(blob.get("hasMore") or blob.get("has_more"))
    return rooms, cursor, more


def _stories_page(payload: Any) -> list[dict[str, Any]]:
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]
    blob = _as_dict(data)
    stories = blob.get("roomStories") or blob.get("room_stories") or blob.get("messages")
    if isinstance(stories, dict):
        edges = stories.get("edges")
        if isinstance(edges, list):
            nodes: list[dict[str, Any]] = []
            for edge in edges:
                node = _as_dict(edge).get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    nodes.append(node)
                elif isinstance(edge, dict) and edge.get("id"):
                    nodes.append(edge)
            return nodes
    if isinstance(stories, list):
        return [item for item in stories if isinstance(item, dict)]
    return []


class UpworkMcpClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.storage = token_storage(self.settings)
        self._org_uid: str | None = None

    async def is_authenticated(self) -> bool:
        return await self.storage.has_tokens()

    @asynccontextmanager
    async def _session(
        self,
        *,
        interactive: bool,
        mode: Literal["cli", "web"] = "cli",
        web_flow: Any | None = None,
    ) -> AsyncIterator[Any]:
        oauth = build_oauth_provider(
            self.settings,
            interactive=interactive,
            storage=self.storage,
            mode=mode,
            web_flow=web_flow,
        )
        async with httpx2.AsyncClient(auth=oauth, follow_redirects=True, timeout=60.0) as http_client:
            async with streamable_http_client(
                self.settings.upwork_mcp_url,
                http_client=http_client,
            ) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def status(self) -> McpStatus:
        if not await self.is_authenticated():
            return McpStatus(connected=False, tools=[], error="Not logged in. Click Connect Upwork on the inbox.")
        try:
            async with self._session(interactive=False) as session:
                listed = await list_session_tools(session)
                names = [getattr(tool, "name", "") for tool in listed]
                return McpStatus(connected=True, tools=names, error="")
        except Exception as exc:
            return McpStatus(connected=False, tools=[], error=format_mcp_error(exc))

    async def login(
        self,
        *,
        mode: Literal["cli", "web"] = "cli",
        web_flow: Any | None = None,
    ) -> list[str]:
        async with self._session(interactive=True, mode=mode, web_flow=web_flow) as session:
            listed = await list_session_tools(session)
            return [getattr(tool, "name", "") for tool in listed]

    async def _resolve_org_uid(self, session: Any) -> str:
        if self._org_uid:
            return self._org_uid
        accounts = _parse_jsonish(_tool_text(await call_session_tool(session, "upwork__list_accounts", {})))
        org_uid = extract_org_uid(accounts)
        if not org_uid:
            raise RuntimeError("Upwork list_accounts did not return an org_uid")
        self._org_uid = org_uid
        return org_uid

    async def search_jobs(self, query: str, *, max_pages: int = 3) -> list[JobPayload]:
        collected: list[JobPayload] = []
        seen: set[str] = set()
        cursor = ""
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)
            for _ in range(max(1, max_pages)):
                params: dict[str, object] = {
                    "query": query,
                    "limit": 10,
                    "sort": "recency",
                }
                if cursor:
                    params["cursor"] = cursor
                result = await call_session_tool(
                    session,
                    "upwork__find_jobs",
                    {"action": "search", "org_uid": org_uid, "params": params},
                )
                for job in jobs_from_tool_result(result):
                    job_id = job.get("id")
                    if not job_id or job_id in seen:
                        continue
                    seen.add(job_id)
                    collected.append(job)
                parsed = _parse_jsonish(_tool_text(result))
                if not page_has_more(parsed):
                    break
                nxt = next_cursor(parsed)
                if not nxt or nxt == cursor:
                    break
                cursor = nxt
        return collected

    async def get_job(self, job_id: str) -> JobPayload | None:
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)
            result = await call_session_tool(
                session,
                "upwork__find_jobs",
                {"action": "get", "org_uid": org_uid, "params": {"id": job_id}},
            )
            jobs = jobs_from_tool_result(result)
            if jobs:
                return jobs[0]
            parsed = _as_dict(_parse_jsonish(_tool_text(result)))
            nested = parsed.get("job") if isinstance(parsed.get("job"), dict) else parsed
            if isinstance(nested, dict):
                return normalize_job(nested)
            return None

    async def enrich_jobs(self, jobs: list[JobPayload]) -> list[JobPayload]:
        if not jobs:
            return jobs
        enriched: list[JobPayload] = []
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)
            for job in jobs:
                try:
                    result = await call_session_tool(
                        session,
                        "upwork__find_jobs",
                        {"action": "get", "org_uid": org_uid, "params": {"id": job["id"]}},
                    )
                    fetched = jobs_from_tool_result(result)
                    detail = fetched[0] if fetched else None
                except Exception:
                    detail = None
                if detail is None:
                    enriched.append(job)
                    continue
                merged: JobPayload = {**job, **detail}
                merged["price_label"] = prefer_price_label(job.get("price_label"), detail.get("price_label"), job.get("budget"), detail.get("budget"))
                merged["budget"] = merged["price_label"] or detail.get("budget") or job.get("budget")
                if not detail.get("job_budget_value") and job.get("job_budget_value"):
                    merged["job_budget_value"] = job["job_budget_value"]
                if job.get("client_payment_status") == "verified" and detail.get("client_payment_status") != "verified":
                    merged["client_payment_status"] = "verified"
                try:
                    left = json.loads(job.get("client_details") or job.get("client") or "{}")
                    right = json.loads(detail.get("client_details") or "{}")
                    if isinstance(left, dict) and isinstance(right, dict):
                        merged_details = merge_client_details(left, right)
                        derive_client_stats(merged_details)
                        merged_details = await store_job_attachments(str(job.get("id") or ""), merged_details)
                        merged_details["job_details_fetched"] = True
                        merged["client_details"] = json.dumps(merged_details, default=str)
                except json.JSONDecodeError:
                    pass
                extra_desc = detail.get("description") or ""
                if len(extra_desc) > len(job.get("description") or ""):
                    merged["description"] = extra_desc
                if job.get("timezone") and not merged.get("timezone"):
                    merged["timezone"] = job["timezone"]
                enriched.append(merged)
        return enriched

    async def get_connects_balance(self) -> int | None:
        try:
            async with self._session(interactive=False) as session:
                await list_session_tools(session)
                org_uid = await self._resolve_org_uid(session)
                result = await call_session_tool(
                    session,
                    "upwork__get_freelancer_financials",
                    {"action": "connects_balance", "org_uid": org_uid, "params": {}},
                )
                return parse_connects_balance(_parse_jsonish(_tool_text(result)))
        except Exception:
            return None

    async def preview_proposal(
        self,
        job_id: str,
        cover_letter: str,
        charged_amount: float,
        boost_connects: int = 0,
    ) -> ConnectsPanel:
        balance = await self.get_connects_balance()
        if not cover_letter.strip():
            return ConnectsPanel(available=balance, charged_amount=charged_amount, error="Cover letter is empty")
        try:
            async with self._session(interactive=False) as session:
                await list_session_tools(session)
                org_uid = await self._resolve_org_uid(session)
                params: dict[str, Any] = {
                    "job_reference": job_id,
                    "cover_letter": cover_letter[:5000],
                    "charged_amount": charged_amount,
                }
                if boost_connects > 0:
                    params["boost_connects"] = boost_connects
                result = await call_session_tool(
                    session,
                    "upwork__manage_proposals",
                    {"action": "create", "org_uid": org_uid, "params": params},
                )
            panel = parse_proposal_preview(_parse_jsonish(_tool_text(result)))
            if panel.available is None:
                panel.available = balance
            if panel.charged_amount is None:
                panel.charged_amount = charged_amount
            return panel
        except Exception as exc:
            detail = format_mcp_error(exc)
            required = connects_required_from_text(detail)
            remaining = None
            if balance is not None and required is not None:
                remaining = balance - required
            return ConnectsPanel(
                available=balance,
                apply_cost=required,
                can_apply=False if connects_shortage(detail) or (remaining is not None and remaining < 0) else None,
                charged_amount=charged_amount,
                error=detail,
                remaining_after_apply=remaining,
            )

    async def list_highlights(self) -> list[ApplyHighlight]:
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)
            result = await call_session_tool(
                session,
                "upwork__get_profile",
                {"action": "list_highlights", "org_uid": org_uid, "params": {}},
            )
            return parse_highlights(_parse_jsonish(_tool_text(result)))

    async def upload_proposal_files(self, files: list[tuple[str, bytes, str]]) -> list[str]:
        uploaded = await self.upload_files(files, context="proposals")
        return [item["file_id"] for item in uploaded if item.get("file_id")]

    async def upload_files(
        self,
        files: list[tuple[str, bytes, str]],
        *,
        context: str = "proposals",
        room_id: str = "",
    ) -> list[dict[str, str]]:
        if not files:
            return []
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)
            params: dict[str, Any] = {
                "context": context,
                "reason": "Dashboard file upload",
            }
            if context == "messages":
                if not room_id:
                    raise RuntimeError("Message attachments need a room_id")
                params["room_id"] = room_id
            started = await call_session_tool(
                session,
                "upwork__start_attachment_upload",
                {"action": "upload", "org_uid": org_uid, "params": params},
            )
            parsed = _as_dict(_parse_jsonish(_tool_text(started)))
            nested = parsed.get("preview") if isinstance(parsed.get("preview"), dict) else parsed
            task_id = _first_str(_as_dict(nested), "task_id") or _first_str(parsed, "task_id")
            if not task_id:
                for blob in (parsed, nested, parsed.get("data")):
                    data = _as_dict(blob)
                    task_id = _first_str(data, "task_id")
                    if task_id:
                        break
                    inner = data.get("upload") if isinstance(data.get("upload"), dict) else {}
                    task_id = _first_str(_as_dict(inner), "task_id")
                    if task_id:
                        break
            if not task_id:
                raise RuntimeError(_tool_text(started) or "Upwork did not start a file upload session")
            encoded = [
                {
                    "name": name,
                    "data": base64.b64encode(data).decode("ascii"),
                    "size": len(data),
                    "type": content_type or "application/octet-stream",
                }
                for name, data, content_type in files
            ]
            stored = await call_session_tool(
                session,
                "upwork__store_uploaded_files",
                {"task_id": task_id, "org_uid": org_uid, "files": encoded},
            )
            uids = _file_uids(_parse_jsonish(_tool_text(stored)))
            for _ in range(20):
                if uids:
                    break
                await asyncio.sleep(0.6)
                status = await call_session_tool(
                    session,
                    "upwork__get_upload_status",
                    {"action": "get", "org_uid": org_uid, "params": {"task_id": task_id}},
                )
                uids = _file_uids(_parse_jsonish(_tool_text(status)))
            if not uids:
                raise RuntimeError(_tool_text(stored) or "Upwork did not return file ids for the upload")
            confirm_params: dict[str, Any] = {"context": context, "file_ids": uids, "task_id": task_id}
            if context == "messages":
                confirm_params["room_id"] = room_id
            try:
                confirmed = await call_session_tool(
                    session,
                    "upwork__confirm_attachment_upload",
                    {"action": "confirm", "org_uid": org_uid, "params": confirm_params},
                )
                confirm_text = _tool_text(confirmed)
                if confirm_text and _skip_attachment_confirm(confirm_text) and uids:
                    logger.warning("confirm_attachment_upload skipped: %s", confirm_text[:400])
            except Exception as exc:
                detail = format_mcp_error(exc)
                if uids and _skip_attachment_confirm(detail):
                    logger.warning("confirm_attachment_upload skipped: %s", detail[:400])
                else:
                    raise
            refs: list[dict[str, str]] = []
            for index, uid in enumerate(uids):
                name = files[index][0] if index < len(files) else uid
                refs.append({"file_id": uid, "file_name": name})
            return refs

    async def list_message_rooms(self, *, max_rooms: int = 40) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        cursor = ""
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)
            for _ in range(8):
                params: dict[str, Any] = {"limit": 10, "room_type": "ALL"}
                if cursor:
                    params["cursor"] = cursor
                result = await call_session_tool(
                    session,
                    "upwork__get_messages",
                    {"action": "list_rooms", "org_uid": org_uid, "params": params},
                )
                payload = _parse_jsonish(_tool_text(result))
                rooms, next_cursor, has_more = _rooms_page(payload)
                collected.extend(rooms)
                if len(collected) >= max_rooms or not has_more or not next_cursor:
                    break
                cursor = next_cursor
        return collected[:max_rooms]

    async def list_room_messages(self, room_id: str, *, limit: int = 40) -> list[dict[str, Any]]:
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)
            result = await call_session_tool(
                session,
                "upwork__get_messages",
                {
                    "action": "list_messages",
                    "org_uid": org_uid,
                    "params": {"room_id": room_id, "limit": min(100, max(1, limit))},
                },
            )
            return _stories_page(_parse_jsonish(_tool_text(result)))

    async def find_message_room(self, context_type: str, context_id: str) -> dict[str, Any] | None:
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)
            result = await call_session_tool(
                session,
                "upwork__get_messages",
                {
                    "action": "find_room",
                    "org_uid": org_uid,
                    "params": {"context_type": context_type, "context_id": context_id},
                },
            )
            parsed = _as_dict(_parse_jsonish(_tool_text(result)))
            data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
            room = data.get("room") if isinstance(data, dict) else None
            if isinstance(room, dict):
                return room
            rooms = data.get("rooms") if isinstance(data, dict) else None
            if isinstance(rooms, list) and rooms and isinstance(rooms[0], dict):
                return rooms[0]
            return None

    async def send_room_message(
        self,
        room_id: str,
        message: str,
        attachments: list[dict[str, str]] | None = None,
    ) -> str:
        outcome = ""
        try:
            async with self._session(interactive=False) as session:
                await list_session_tools(session)
                org_uid = await self._resolve_org_uid(session)
                params: dict[str, Any] = {"room_id": room_id}
                text = message.strip()
                if text:
                    params["message"] = text[:10240]
                files = [item for item in (attachments or []) if item.get("file_id")]
                if files:
                    packed: list[dict[str, str]] = []
                    for item in files:
                        row: dict[str, str] = {
                            "file_id": item["file_id"],
                            "file_name": item.get("file_name") or item["file_id"],
                        }
                        if item.get("image_id"):
                            row["image_id"] = item["image_id"]
                        packed.append(row)
                    params["file_attachments"] = packed
                if not params.get("message") and not params.get("file_attachments"):
                    raise RuntimeError("Message is empty")
                created = await call_session_tool(
                    session,
                    "upwork__send_message",
                    {"action": "send", "org_uid": org_uid, "params": params},
                )
                created_text = _tool_text(created)
                parsed = _as_dict(_parse_jsonish(created_text))
                draft_id, draft_type = _write_draft(parsed)
                if not draft_id:
                    if _looks_sent(parsed, created_text):
                        outcome = created_text or "sent"
                        return outcome
                    raise RuntimeError(created_text or "Upwork did not return a message draft")
                types = [draft_type] if draft_type else []
                for fallback in ("message", "send_message", "room_message"):
                    if fallback not in types:
                        types.append(fallback)
                last_error = ""
                for kind in types:
                    try:
                        confirmed = await call_session_tool(
                            session,
                            "upwork__confirm_draft",
                            {
                                "action": "confirm",
                                "org_uid": org_uid,
                                "params": {"type": kind, "draft_id": draft_id},
                            },
                        )
                        outcome = _tool_text(confirmed) or "sent"
                        return outcome
                    except Exception as exc:
                        last_error = format_mcp_error(exc)
                        continue
                raise RuntimeError(last_error or created_text or "Could not confirm the message draft")
        except Exception as exc:
            if outcome:
                return outcome
            raise RuntimeError(format_mcp_error(exc)) from exc

    async def submit_proposal(
        self,
        job_id: str,
        cover_letter: str,
        charged_amount: float,
        boost_connects: int = 0,
        milestones: list[ProposalMilestone] | None = None,
        answers: list[ScreeningAnswer] | None = None,
        portfolio_project_ids: list[str] | None = None,
        certificate_ids: list[str] | None = None,
        attachments: list[str] | None = None,
    ) -> str:
        outcome = ""
        try:
            async with self._session(interactive=False) as session:
                await list_session_tools(session)
                org_uid = await self._resolve_org_uid(session)
                params: dict[str, Any] = {
                    "job_reference": job_id,
                    "cover_letter": cover_letter[:5000],
                    "charged_amount": charged_amount,
                }
                if boost_connects > 0:
                    params["boost_connects"] = boost_connects
                if answers:
                    params["answers"] = [
                        {"question": item.question, "answer": item.answer}
                        for item in answers
                        if item.question.strip() and item.answer.strip()
                    ]
                if portfolio_project_ids:
                    params["portfolio_project_ids"] = [item for item in portfolio_project_ids if item]
                if certificate_ids:
                    params["certificate_ids"] = [item for item in certificate_ids if item]
                if attachments:
                    params["attachments"] = [item for item in attachments if item]
                created = await call_session_tool(
                    session,
                    "upwork__manage_proposals",
                    {"action": "create", "org_uid": org_uid, "params": params},
                )
                created_text = _tool_text(created)
                if already_applied(created_text):
                    outcome = "already_applied"
                    return outcome
                parsed = _as_dict(_parse_jsonish(created_text))
                preview = parse_proposal_preview(parsed)
                if already_applied(preview.error):
                    outcome = "already_applied"
                    return outcome
                apply_cost = preview.apply_cost or 0
                available = preview.available
                total = apply_cost + max(0, boost_connects)
                if available is not None and total > available:
                    raise RuntimeError(f"Not enough Connects: need {total}, have {available}")
                draft_id = str(parsed.get("draft_id") or "")
                if not draft_id:
                    raise RuntimeError(created_text or "Upwork did not return a proposal draft")
                milestone_payload = mcp_milestone_params(milestones or [])
                attached = 0
                if milestone_payload and preview.milestones_allowed:
                    updated = await call_session_tool(
                        session,
                        "upwork__update_draft",
                        {
                            "action": "update",
                            "org_uid": org_uid,
                            "params": {
                                "type": "proposal",
                                "id": draft_id,
                                "content": {"milestones": milestone_payload},
                                "mode": "merge",
                            },
                        },
                    )
                    updated_parsed = _as_dict(_parse_jsonish(_tool_text(updated)))
                    new_id = str(updated_parsed.get("draft_id") or "")
                    if not new_id:
                        raise RuntimeError(_tool_text(updated) or "Upwork did not return an updated proposal draft")
                    draft_id = new_id
                    attached = len(milestone_payload)
                confirmed = await call_session_tool(
                    session,
                    "upwork__confirm_draft",
                    {
                        "action": "confirm",
                        "org_uid": org_uid,
                        "params": {"type": "proposal", "draft_id": draft_id},
                    },
                )
                text = _tool_text(confirmed) or "submitted"
                outcome = f"apply={apply_cost} boost={boost_connects} milestones={attached} {text}"
                return outcome
        except Exception as exc:
            if outcome:
                return outcome
            detail = format_mcp_error(exc)
            if already_applied(detail):
                return "already_applied"
            raise

    async def dump_tools(self, *needles: str) -> list[tuple[str, Any]]:
        dumps: list[tuple[str, Any]] = []
        async with self._session(interactive=False) as session:
            tools = await list_session_tools(session)
            for tool in tools:
                name = getattr(tool, "name", "") or ""
                lowered = name.lower().replace("-", "_")
                if needles and not any(needle in lowered for needle in needles):
                    continue
                try:
                    result = await call_session_tool(session, name, {})
                    dumps.append((name, _parse_jsonish(_tool_text(result))))
                except Exception as exc:
                    dumps.append((name, {"error": str(exc)}))
        return dumps

    async def list_tool_names(self) -> list[str]:
        async with self._session(interactive=False) as session:
            tools = await list_session_tools(session)
            return [getattr(tool, "name", "") for tool in tools]

    async def call_named_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            result = await call_session_tool(session, name, arguments or {})
            return _parse_jsonish(_tool_text(result))

    async def fetch_proposal_pages(
        self,
        statuses: tuple[str, ...] | list[str],
        *,
        max_pages: int = 3,
    ) -> list[Any]:
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            org_uid = await self._resolve_org_uid(session)

            async def named(tool: str, action: str, params: dict[str, Any] | None = None) -> Any:
                args: dict[str, Any] = {"action": action, "org_uid": org_uid, "params": params or {}}
                try:
                    result = await call_session_tool(session, tool, args)
                    return _parse_jsonish(_tool_text(result))
                except Exception as exc:
                    return {"error": str(exc), "tool": tool, "action": action}

            return await paginate_freelancer_proposals(named, tuple(statuses), max_pages=max_pages)

    async def fetch_work_memory(self) -> dict[str, Any]:
        async with self._session(interactive=False) as session:
            await list_session_tools(session)
            accounts = _parse_jsonish(_tool_text(await call_session_tool(session, "upwork__list_accounts", {})))
            org_uid = extract_org_uid(accounts)
            if not org_uid:
                raise RuntimeError("Upwork list_accounts did not return an org_uid")

            async def named(tool: str, action: str, params: dict[str, Any] | None = None) -> Any:
                args: dict[str, Any] = {"action": action, "org_uid": org_uid, "params": params or {}}
                try:
                    result = await call_session_tool(session, tool, args)
                    return _parse_jsonish(_tool_text(result))
                except Exception as exc:
                    return {"error": str(exc), "tool": tool, "action": action}

            profile = await named("upwork__get_profile", "get")
            highlights = await named("upwork__get_profile", "list_highlights")
            account = await named("upwork__get_account", "get_user_details")
            contracts: list[Any] = []
            offset = 0
            for _ in range(15):
                page = await named(
                    "upwork__list_contracts",
                    "search",
                    {
                        "limit": 10,
                        "offset": offset,
                        "contract_statuses": ["ACTIVE", "CLOSED", "PAUSED"],
                    },
                )
                contracts.append(page)
                if page.get("error") or not page_has_more(page):
                    break
                offset += 10
            proposals = await paginate_freelancer_proposals(
                named, PROPOSAL_MEMORY_STATUSES, max_pages=8
            )
            applications = list(proposals)
            applications.extend(
                await paginate_freelancer_proposals(named, PROPOSAL_CLOSED_STATUSES, max_pages=8)
            )
            return {
                "org_uid": org_uid,
                "accounts": accounts,
                "profile": profile,
                "highlights": highlights,
                "account": account,
                "contracts": contracts,
                "proposals": proposals,
                "applications": applications,
            }
