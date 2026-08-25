from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import FeedbackLog, Job, PortfolioItem, PreferenceRule
from app.db.session import SessionLocal
from app.embeddings import add_embedding, portfolio_blob, remove_embedding
from app.models import (
    EmbeddingSource,
    FeedbackOutcome,
    LearnPreferenceArgs,
    LogFeedbackArgs,
    PreferenceRuleOut,
    PortfolioItemOut,
    ToolSpec,
    UpdatePortfolioArgs,
    WorkKind,
    WorkOrigin,
)
from app.tools import register_tool
from app.events import add_event


def _session(db: Session | None) -> tuple[Session, bool]:
    if db is not None:
        return db, False
    return SessionLocal(), True


async def learn_preference(
    category: str,
    rule: str,
    enforcement_level: str = "soft_penalty",
    db: Session | None = None,
) -> PreferenceRuleOut:
    args = LearnPreferenceArgs.model_validate(
        {"category": category, "rule": rule, "enforcement_level": enforcement_level}
    )
    session, own = _session(db)
    try:
        row = PreferenceRule(
            category=args.category.value,
            rule=args.rule.strip(),
            enforcement_level=args.enforcement_level.value,
            active=True,
        )
        session.add(row)
        session.flush()
        add_event(session, "preference", f"{row.category}: {row.rule}")
        if own:
            session.commit()
            session.refresh(row)
        return PreferenceRuleOut(
            id=row.id,
            category=row.category,
            rule=row.rule,
            enforcement_level=row.enforcement_level,
            active=row.active,
        )
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


async def update_portfolio_matrix(
    project_title: str,
    tech_stack: list[str] | None = None,
    outcomes_achieved: str = "",
    associated_keywords: list[str] | None = None,
    description: str = "",
    kind: str = "project",
    db: Session | None = None,
) -> PortfolioItemOut:
    args = UpdatePortfolioArgs(
        project_title=project_title,
        tech_stack=tech_stack or [],
        outcomes_achieved=outcomes_achieved,
        associated_keywords=associated_keywords or [],
        description=description,
        kind=WorkKind(kind) if kind in {item.value for item in WorkKind} else WorkKind.project,
    )
    session, own = _session(db)
    try:
        row = PortfolioItem(
            project_title=args.project_title,
            tech_stack=json.dumps(args.tech_stack),
            outcomes_achieved=args.outcomes_achieved,
            associated_keywords=json.dumps(args.associated_keywords),
            origin=WorkOrigin.agent.value,
            kind=args.kind.value,
            description=args.description,
        )
        session.add(row)
        session.flush()
        add_embedding(session, EmbeddingSource.portfolio, row.id, portfolio_blob(row))
        add_event(session, "portfolio", f"Added {row.project_title}")
        if own:
            session.commit()
            session.refresh(row)
        return PortfolioItemOut(
            id=row.id,
            project_title=row.project_title,
            tech_stack=args.tech_stack,
            outcomes_achieved=row.outcomes_achieved,
            associated_keywords=args.associated_keywords,
            origin=row.origin,
            kind=row.kind,
            description=row.description,
            editable=True,
        )
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


def _item_out(row: PortfolioItem) -> PortfolioItemOut:
    try:
        stack = json.loads(row.tech_stack)
    except json.JSONDecodeError:
        stack = []
    try:
        keywords = json.loads(row.associated_keywords)
    except json.JSONDecodeError:
        keywords = []
    return PortfolioItemOut(
        id=row.id,
        project_title=row.project_title,
        tech_stack=stack if isinstance(stack, list) else [],
        outcomes_achieved=row.outcomes_achieved,
        associated_keywords=keywords if isinstance(keywords, list) else [],
        origin=row.origin,
        kind=row.kind,
        description=row.description or "",
        editable=row.origin == WorkOrigin.agent.value,
    )


async def save_agent_item(
    item_id: int,
    project_title: str,
    tech_stack: list[str] | None = None,
    outcomes_achieved: str = "",
    associated_keywords: list[str] | None = None,
    description: str = "",
    kind: str = "project",
    db: Session | None = None,
) -> PortfolioItemOut | None:
    session, own = _session(db)
    try:
        row = session.query(PortfolioItem).filter(PortfolioItem.id == item_id).one_or_none()
        if row is None or row.origin != WorkOrigin.agent.value:
            return None
        row.project_title = project_title.strip()
        row.tech_stack = json.dumps(tech_stack or [])
        row.outcomes_achieved = outcomes_achieved
        row.associated_keywords = json.dumps(associated_keywords or [])
        row.description = description
        if kind in {item.value for item in WorkKind}:
            row.kind = kind
        session.flush()
        add_embedding(session, EmbeddingSource.portfolio, row.id, portfolio_blob(row))
        add_event(session, "portfolio", f"Updated {row.project_title}")
        if own:
            session.commit()
            session.refresh(row)
        return _item_out(row)
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


async def upsert_upwork_work(
    external_id: str,
    project_title: str,
    description: str = "",
    outcomes_achieved: str = "",
    tech_stack: list[str] | None = None,
    associated_keywords: list[str] | None = None,
    kind: str = "job_history",
    embed: bool = True,
    db: Session | None = None,
) -> PortfolioItemOut:
    session, own = _session(db)
    try:
        row = session.query(PortfolioItem).filter(PortfolioItem.external_id == external_id).one_or_none()
        created = row is None
        if row is None:
            row = PortfolioItem(
                origin=WorkOrigin.upwork.value,
                external_id=external_id,
            )
            session.add(row)
        row.project_title = project_title.strip() or "Upwork work"
        row.description = description
        row.outcomes_achieved = outcomes_achieved
        row.tech_stack = json.dumps(tech_stack or [])
        row.associated_keywords = json.dumps(associated_keywords or [])
        row.kind = kind if kind in {item.value for item in WorkKind} else WorkKind.job_history.value
        row.origin = WorkOrigin.upwork.value
        row.synced_at = datetime.now(UTC)
        session.flush()
        if embed:
            add_embedding(session, EmbeddingSource.portfolio, row.id, portfolio_blob(row))
        else:
            remove_embedding(session, EmbeddingSource.portfolio, row.id)
        if created:
            add_event(session, "upwork", f"Imported {row.project_title}")
        if own:
            session.commit()
            session.refresh(row)
        return _item_out(row)
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


def prune_upwork_work(keep_external_ids: set[str], db: Session) -> int:
    rows = db.query(PortfolioItem).filter(PortfolioItem.origin == WorkOrigin.upwork.value).all()
    removed = 0
    for row in rows:
        if row.external_id in keep_external_ids:
            continue
        remove_embedding(db, EmbeddingSource.portfolio, row.id)
        db.delete(row)
        removed += 1
    return removed


async def log_interaction_feedback(
    job_id: str,
    outcome: str,
    client_notes: str = "",
    db: Session | None = None,
) -> dict[str, str | int | None]:
    args = LogFeedbackArgs.model_validate(
        {"job_id": job_id, "outcome": outcome, "client_notes": client_notes}
    )
    session, own = _session(db)
    try:
        job: Job | None = None
        if args.job_id.isdigit():
            job = session.query(Job).filter(Job.id == int(args.job_id)).one_or_none()
        if job is None:
            job = session.query(Job).filter(Job.upwork_id == args.job_id).one_or_none()
        row = FeedbackLog(
            job_id=job.id if job else None,
            upwork_id=job.upwork_id if job else args.job_id,
            outcome=args.outcome.value,
            client_notes=args.client_notes,
        )
        session.add(row)
        session.flush()
        if job is not None:
            add_event(session, "feedback", f"{args.outcome.value}: {args.client_notes[:400]}", job.id)
            if args.outcome in {FeedbackOutcome.hired, FeedbackOutcome.shortlisted, FeedbackOutcome.approved}:
                add_embedding(
                    session,
                    EmbeddingSource.job,
                    job.id,
                    f"{job.title}\n{job.description}",
                )
        if own:
            session.commit()
        return {
            "id": row.id,
            "job_id": job.id if job else None,
            "outcome": args.outcome.value,
        }
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


register_tool(
    ToolSpec(
        name="learn_preference",
        description="Save an implicit or explicit hiring rule for future scoring.",
        parameters={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["budget", "tech_stack", "client_metrics", "proposal_style"],
                },
                "rule": {"type": "string"},
                "enforcement_level": {
                    "type": "string",
                    "enum": ["strict_block", "soft_penalty"],
                },
            },
            "required": ["category", "rule"],
        },
    ),
    learn_preference,
)

register_tool(
    ToolSpec(
        name="update_portfolio_matrix",
        description="Add a case study, repo, or skill milestone to agent memory.",
        parameters={
            "type": "object",
            "properties": {
                "project_title": {"type": "string"},
                "tech_stack": {"type": "array", "items": {"type": "string"}},
                "outcomes_achieved": {"type": "string"},
                "associated_keywords": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
            },
            "required": ["project_title"],
        },
    ),
    update_portfolio_matrix,
)

register_tool(
    ToolSpec(
        name="log_interaction_feedback",
        description="Record why a proposal succeeded or failed so the agent stops repeating errors.",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["hired", "shortlisted", "viewed", "ignored", "rejected", "approved", "edited", "expired"],
                },
                "client_notes": {"type": "string"},
            },
            "required": ["job_id", "outcome"],
        },
    ),
    log_interaction_feedback,
)
