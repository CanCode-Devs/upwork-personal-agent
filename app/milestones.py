from __future__ import annotations

import json
from typing import Any

from app.db.models import Job, Proposal
from app.models import ProposalMilestone
from app.proposal_writer import letter_has_plan


def _job_type(job: Job) -> str:
    return (job.job_type or "").lower()


def job_needs_milestones(job: Job, bid: float = 0.0, allowed: bool | None = None) -> bool:
    _ = bid
    if allowed is False:
        return False
    return _job_type(job) in {"fixed", "fixed_price"}


def load_milestones(proposal: Proposal | None) -> list[ProposalMilestone]:
    if proposal is None:
        return []
    raw = getattr(proposal, "milestones_json", "") or "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[ProposalMilestone] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        try:
            items.append(ProposalMilestone.model_validate(row))
        except Exception:
            continue
    return items


def dump_milestones(items: list[ProposalMilestone]) -> str:
    return json.dumps([item.model_dump() for item in items], default=str)


def parse_milestone_form(descriptions: list[str], amounts: list[str], titles: list[str] | None = None) -> list[ProposalMilestone]:
    titles = titles or []
    items: list[ProposalMilestone] = []
    count = max(len(descriptions), len(amounts), len(titles))
    for index in range(count):
        description = descriptions[index].strip() if index < len(descriptions) else ""
        title = titles[index].strip() if index < len(titles) else ""
        raw_amount = amounts[index].strip() if index < len(amounts) else ""
        if not description and not title and not raw_amount:
            continue
        try:
            amount = float(raw_amount.replace(",", "").replace("$", ""))
        except ValueError:
            continue
        if amount <= 0:
            continue
        text = description or title
        if not text:
            continue
        items.append(ProposalMilestone(title=title, description=text, amount=amount))
    return items


def align_milestone_total(items: list[ProposalMilestone], total: float) -> list[ProposalMilestone]:
    if not items or total <= 0:
        return items
    current = sum(item.amount for item in items)
    if current <= 0:
        return items
    scaled = [ProposalMilestone(title=item.title, description=item.description, amount=round(item.amount * total / current, 2)) for item in items]
    drift = round(total - sum(item.amount for item in scaled), 2)
    scaled[-1] = ProposalMilestone(
        title=scaled[-1].title,
        description=scaled[-1].description,
        amount=round(scaled[-1].amount + drift, 2),
    )
    return scaled


def heuristic_milestones(job: Job, total: float) -> list[ProposalMilestone]:
    title = (job.title or "this project").strip()
    items = [
        ProposalMilestone(
            title="Discovery",
            description=f"Confirm scope, sample data, and success checks for {title}.",
            amount=round(total * 0.2, 2),
        ),
        ProposalMilestone(
            title="Core delivery",
            description="Build and validate the working system against the agreed sample.",
            amount=round(total * 0.55, 2),
        ),
        ProposalMilestone(
            title="Handoff",
            description="Docs, deploy notes, and a walkthrough so you can run it.",
            amount=round(total * 0.25, 2),
        ),
    ]
    return align_milestone_total(items, total)


def mcp_milestone_params(items: list[ProposalMilestone]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in items:
        label = item.description.strip()
        if item.title and item.title.lower() not in label.lower():
            label = f"{item.title}: {label}"
        if not label:
            continue
        payload.append({"description": label[:240], "amount": round(item.amount, 2)})
    return payload


def append_milestone_section(letter: str, items: list[ProposalMilestone]) -> str:
    if not items:
        return letter
    if letter_has_plan(letter):
        return letter
    lines = ["", "Proposed milestones:", ""]
    for index, item in enumerate(items, 1):
        heading = item.title or item.description
        lines.append(f"{index}. {heading}: ${item.amount:g}")
        if item.title and item.description and item.description != item.title:
            lines.append(f"   {item.description}")
    return letter.rstrip() + "\n" + "\n".join(lines)


def coerce_milestones(raw: Any, total: float) -> list[ProposalMilestone]:
    if not isinstance(raw, list):
        return []
    items: list[ProposalMilestone] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            items.append(ProposalMilestone.model_validate(row))
        except Exception:
            continue
    items = [item for item in items if item.amount > 0 and (item.description or item.title).strip()]
    if not items:
        return []
    return align_milestone_total(items, total)
