from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Job, UpworkApplication, UpworkProfile
from app.embeddings import rebuild_portfolio_embeddings
from app.events import add_event
from app.models import WorkKind
from app.tools.memory import prune_upwork_work, upsert_upwork_work
from app.upwork.mcp_client import UpworkMcpClient

_UNTRUSTED = re.compile(r"</?untrusted_participant_content>\s*", re.IGNORECASE)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _UNTRUSTED.sub("", value).strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "name", "title", "prettyName", "displayValue", "label", "status"):
            found = _text(value.get(key))
            if found:
                return found
        return ""
    if isinstance(value, list):
        return " ".join(part for part in (_text(item) for item in value) if part).strip()
    return str(value).strip()


def _pick(data: dict[str, Any], *names: str) -> Any:
    lower = {k.lower().replace("-", "_"): v for k, v in data.items()}
    for name in names:
        key = name.lower().replace("-", "_")
        if key in lower and lower[key] not in (None, ""):
            return lower[key]
    return None


def _skills_from(data: dict[str, Any]) -> list[str]:
    raw = _pick(data, "skills", "skillNames", "occupations", "skillSet")
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        names: list[str] = []
        for item in raw:
            if isinstance(item, str):
                names.append(item.strip())
            elif isinstance(item, dict):
                name = _text(_pick(item, "prettyName", "name", "skill", "title"))
                if name:
                    names.append(name)
        return [name for name in names if name]
    return []


def _hourly_from(data: dict[str, Any]) -> int | None:
    raw = _pick(data, "hourlyRate", "hourly_rate", "rate", "billRate", "chargeRate")
    if isinstance(raw, dict):
        raw = _pick(raw, "rawValue", "amount", "max", "min")
    try:
        if raw is not None:
            return int(float(str(raw).replace("$", "").strip()))
    except (TypeError, ValueError):
        return None
    return None


def _as_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("data")
    return nested if isinstance(nested, dict) else payload


def _record(
    *,
    external_id: str,
    title: str,
    description: str = "",
    status: str = "",
    skills: list[str] | None = None,
    kind: str,
) -> dict[str, Any] | None:
    title = _text(title)
    if not title or not external_id:
        return None
    return {
        "external_id": str(external_id),
        "title": title,
        "description": _text(description),
        "status": _text(status),
        "skills": skills or [],
        "kind": kind,
    }


def _profile_fields(payloads: dict[str, Any]) -> dict[str, Any]:
    data = _as_data(payloads.get("profile"))
    personal = data.get("personalData") if isinstance(data.get("personalData"), dict) else {}
    chosen: dict[str, Any] = {
        "title": _text(_pick(personal, "title") or _pick(data, "title")),
        "overview": _text(_pick(personal, "description", "overview") or _pick(data, "description", "overview")),
        "skills": _skills_from(data) or _skills_from(personal),
        "hourly": _hourly_from(personal) or _hourly_from(data),
    }
    return chosen


def _from_employment(payload: Any) -> list[dict[str, Any]]:
    data = _as_data(payload)
    rows = data.get("employmentRecords")
    if not isinstance(rows, list):
        return []
    found: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        record = _record(
            external_id=str(_pick(item, "id") or ""),
            title=_text(_pick(item, "jobTitle", "title")),
            description=_text(_pick(item, "companyName", "company")),
            status="employment",
            kind=WorkKind.employment.value,
        )
        if record:
            found.append(record)
    return found


def _from_highlights(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    found: list[dict[str, Any]] = []
    projects = payload.get("portfolio_projects")
    if isinstance(projects, list):
        for item in projects:
            if not isinstance(item, dict):
                continue
            record = _record(
                external_id=str(_pick(item, "id") or ""),
                title=_text(_pick(item, "title", "name")),
                description="Upwork portfolio project",
                kind=WorkKind.project.value,
            )
            if record:
                found.append(record)
    return found


def _from_contracts(payloads: list[Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for payload in payloads:
        data = _as_data(payload)
        vendor = data.get("vendorContracts") if isinstance(data.get("vendorContracts"), dict) else data
        rows = vendor.get("contracts") if isinstance(vendor, dict) else None
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            client = item.get("clientOrganization") if isinstance(item.get("clientOrganization"), dict) else {}
            client_name = _text(_pick(client, "name"))
            status = _text(_pick(item, "status"))
            title = _text(_pick(item, "title"))
            bits = [part for part in (f"Client: {client_name}" if client_name else "", f"Status: {status}" if status else "") if part]
            record = _record(
                external_id=str(_pick(item, "id") or ""),
                title=title,
                description=". ".join(bits),
                status=status,
                kind=WorkKind.job_history.value,
            )
            if record:
                found.append(record)
    return found


def _from_proposals(payloads: list[Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for payload in payloads:
        data = _as_data(payload)
        vendor = data.get("vendorProposals") if isinstance(data.get("vendorProposals"), dict) else {}
        edges = vendor.get("edges") if isinstance(vendor, dict) else None
        if not isinstance(edges, list):
            continue
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                continue
            posting = node.get("marketplaceJobPosting") if isinstance(node.get("marketplaceJobPosting"), dict) else {}
            content = posting.get("content") if isinstance(posting.get("content"), dict) else {}
            title = _text(_pick(content, "title") or _pick(posting, "title") or _pick(node, "title"))
            status_raw = node.get("status")
            status = ""
            if isinstance(status_raw, dict):
                status = _text(_pick(status_raw, "status_label", "status"))
            else:
                status = _text(status_raw)
            lowered = status.lower()
            if lowered in {"withdrawn", "declined", "rejected"}:
                continue
            terms = node.get("terms") if isinstance(node.get("terms"), dict) else {}
            rate = _text(_pick(terms.get("chargeRate") if isinstance(terms.get("chargeRate"), dict) else {}, "displayValue"))
            bits = [
                "Upwork proposal — not a completed contract",
                status,
                rate,
            ]
            record = _record(
                external_id=str(_pick(node, "id") or _pick(posting, "id") or ""),
                title=title,
                description=". ".join(part for part in bits if part),
                status=status,
                kind=WorkKind.proposal.value,
            )
            if record:
                found.append(record)
    return found


def _from_applications(payloads: list[Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for payload in payloads:
        data = _as_data(payload)
        vendor = data.get("vendorProposals") if isinstance(data.get("vendorProposals"), dict) else {}
        edges = vendor.get("edges") if isinstance(vendor, dict) else None
        if not isinstance(edges, list):
            continue
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                continue
            posting = node.get("marketplaceJobPosting") if isinstance(node.get("marketplaceJobPosting"), dict) else {}
            content = posting.get("content") if isinstance(posting.get("content"), dict) else {}
            posting_id = str(_pick(posting, "id") or "")
            if not posting_id or posting_id in seen:
                continue
            seen.add(posting_id)
            status_raw = node.get("status")
            if isinstance(status_raw, dict):
                status = _text(_pick(status_raw, "status_label", "status"))
            else:
                status = _text(status_raw)
            terms = node.get("terms") if isinstance(node.get("terms"), dict) else {}
            rate = _text(_pick(terms.get("chargeRate") if isinstance(terms.get("chargeRate"), dict) else {}, "displayValue"))
            found.append(
                {
                    "posting_id": posting_id,
                    "title": _text(_pick(content, "title") or _pick(posting, "title") or _pick(node, "title")),
                    "status": status,
                    "rate": rate,
                }
            )
    return found


def upsert_applications(db: Session, rows: list[dict[str, str]]) -> int:
    keep: set[str] = set()
    for row in rows:
        posting_id = row["posting_id"]
        keep.add(posting_id)
        existing = db.query(UpworkApplication).filter(UpworkApplication.posting_id == posting_id).one_or_none()
        if existing is None:
            existing = UpworkApplication(posting_id=posting_id)
            db.add(existing)
        existing.title = row["title"]
        existing.status = row["status"]
        existing.rate = row["rate"]
        existing.synced_at = datetime.now(UTC)
    if keep:
        stale = db.query(UpworkApplication).filter(~UpworkApplication.posting_id.in_(keep)).all()
        for item in stale:
            db.delete(item)
    for job in db.query(Job).all():
        job.applied_on_upwork = job.upwork_id in keep or job.status == "submitted"
    return len(keep)


async def sync_upwork_memory(db: Session, client: UpworkMcpClient | None = None) -> dict[str, int]:
    client = client or UpworkMcpClient()
    if not await client.is_authenticated():
        raise RuntimeError("Upwork MCP is not logged in")
    payloads = await client.fetch_work_memory()
    records: list[dict[str, Any]] = []
    records.extend(_from_employment(payloads.get("profile")))
    records.extend(_from_highlights(payloads.get("highlights")))
    records.extend(_from_contracts(payloads.get("contracts") or []))
    records.extend(_from_proposals(payloads.get("proposals") or []))
    imported = 0
    seen: set[str] = set()
    for record in records:
        external_id = f"upwork:{record['kind']}:{record['external_id']}"
        if external_id in seen:
            continue
        seen.add(external_id)
        await upsert_upwork_work(
            external_id=external_id,
            project_title=record["title"],
            description=record["description"],
            outcomes_achieved=record["status"],
            tech_stack=record["skills"],
            kind=record["kind"],
            embed=False,
            db=db,
        )
        imported += 1
    prune_upwork_work(seen, db)
    rebuild_portfolio_embeddings(db)
    applied = upsert_applications(db, _from_applications(payloads.get("applications") or payloads.get("proposals") or []))

    profile_blob = _profile_fields(payloads)
    if any(profile_blob.values()):
        row = db.query(UpworkProfile).order_by(UpworkProfile.id.asc()).first()
        if row is None:
            row = UpworkProfile()
            db.add(row)
        row.title = str(profile_blob.get("title") or row.title or "")
        row.overview = str(profile_blob.get("overview") or row.overview or "")
        skills = profile_blob.get("skills")
        if isinstance(skills, list) and skills:
            row.skills_json = json.dumps(skills)
        hourly = profile_blob.get("hourly")
        if isinstance(hourly, int):
            row.hourly_rate = hourly
        row.raw_json = json.dumps(
            {
                "org_uid": payloads.get("org_uid"),
                "identity": _as_data(payloads.get("profile")).get("identity"),
                "aggregates": _as_data(payloads.get("profile")).get("profileAggregates"),
            },
            default=str,
        )
        row.synced_at = datetime.now(UTC)
    add_event(db, "upwork", f"Synced {imported} read-only Upwork records, {applied} applications")
    return {"imported": imported, "applications": applied}
