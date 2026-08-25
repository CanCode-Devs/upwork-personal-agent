from __future__ import annotations

import json
from typing import Sequence

from sqlalchemy.orm import Session

from app.db.models import PreferenceRule, ProposalExample, ProposalSettings
from app.db.session import SessionLocal
from app.embeddings import cosine, embed_texts
from app.models import FreelancerProfile, MilestoneStageConfig, StyleExample, WriterConfig
from app.proposal_writer import OPENING_HOOK

DEFAULT_LETTER_STRUCTURE = """Opening hook
Technical hook: 1-2 lines on what you would actually build for THIS job
Hardest part: the risk or engineering effort beyond the job-post happy path
Solution: 1-3 sentences on the approach
Relevant work: ONE project from the provided proof. Do not invent contracts, metrics, or clients
Posting apply items: if job.apply_questions is non-empty, add one labeled section per item AFTER relevant work and BEFORE the offer/CTA
Offer: a low-risk reason to start talking, only when the job is substantial
Direct CTA: tell them what to send or do next. If a client timezone and working hours are provided, make availability specific to those. Do not invent hours"""

DEFAULT_ROLE_LETTER_STRUCTURE = """Opening hook
Role read: 1-2 lines showing you understood they are hiring a person for ongoing work, not a one-off build
Fit: how you work vs their how-we-work (async, AI-with vs without, with a team)
Proof: ONE similar seat or platform from the provided proof. Do not invent a delivery plan, architecture, or escrow stages
Trial/CTA: answer their trial, start date, hours, or links ask. Do not invent hours"""

DEFAULT_NEVER_SAY = """excited to apply
would love to work
perfect candidate
great fit
extensive experience
looking forward to hearing
passionate about
proven track record
cutting-edge
revolutionary
seamless
robust
next-generation
innovative
world-class"""

DEFAULT_MILESTONE_INSTRUCTIONS = (
    "Return milestones as an array of objects with keys title, description, amount (number). "
    "Stages must sum to the given bid. Do not mention those milestones, stage lists, or dollar amounts in cover_letter."
)

DEFAULT_SCREENING_INSTRUCTIONS = (
    "Answer official Upwork screening questions in screening_answers, not in the cover letter. "
    "Be specific to this job. No buzzwords. No em dashes."
)

DEFAULT_APPLY_INSTRUCTIONS = (
    "If job.apply_questions is non-empty, add one labeled section per item in cover_letter after relevant work "
    "and before the offer/CTA. Use the item text as the heading. Answer with real proof from the provided "
    "profile/proof only. Never invent GitHub URLs, repos, demos, case studies, or metrics. If you cannot prove "
    "an item, say so in one sentence and name the closest real work. These answers belong in cover_letter, not in screening_answers."
)

DEFAULT_MILESTONE_STAGES = [
    MilestoneStageConfig(title="Discovery", weight=20.0, description="Confirm scope, sample data, and success checks."),
    MilestoneStageConfig(title="Core delivery", weight=55.0, description="Build and validate the working system against the agreed sample."),
    MilestoneStageConfig(title="Handoff", weight=25.0, description="Docs, deploy notes, and a walkthrough so you can run it."),
]

JSON_OUTPUT_RULES = """Return JSON only with keys:
- cover_letter (string)
- milestones (array of {title, description, amount} summing to the given bid when milestones are requested; otherwise [])
- screening_answers (array of {question, answer} for each provided screening question; otherwise [])

Never put escrow milestones, numbered delivery stages, or dollar amounts for stages in the cover letter. Upwork has a dedicated milestones form; return those only in the milestones JSON field.
If job.attachment_text is present, use those requirements in the technical hook. Do not quote long passages.
If job.client_first_name is provided, you may use it in the CTA. Never invent a name.
Never paste the client's job description back at them.
Separate every section with a blank line. The cover_letter MUST contain real paragraph breaks (newline + newline). Never return a single wall of text.
No em dashes. No decorative quotation marks around ordinary phrases."""


def default_writer_config() -> WriterConfig:
    return WriterConfig(
        opening_hook=OPENING_HOOK,
        enforce_opening_hook=bool(OPENING_HOOK.strip()),
        tone="consultative",
        letter_structure=DEFAULT_LETTER_STRUCTURE,
        role_letter_structure=DEFAULT_ROLE_LETTER_STRUCTURE,
        must_include="",
        never_say=DEFAULT_NEVER_SAY,
        extra_instructions="",
        target_words=None,
        milestone_instructions=DEFAULT_MILESTONE_INSTRUCTIONS,
        milestone_stages=list(DEFAULT_MILESTONE_STAGES),
        milestone_min=3,
        milestone_max=5,
        screening_instructions=DEFAULT_SCREENING_INSTRUCTIONS,
        apply_questions_instructions=DEFAULT_APPLY_INSTRUCTIONS,
        example_count=2,
    )


def parse_milestone_stages(raw: str) -> list[MilestoneStageConfig]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return list(DEFAULT_MILESTONE_STAGES)
    if not isinstance(parsed, list):
        return list(DEFAULT_MILESTONE_STAGES)
    items: list[MilestoneStageConfig] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        try:
            items.append(MilestoneStageConfig.model_validate(row))
        except Exception:
            continue
    return items or list(DEFAULT_MILESTONE_STAGES)


def dump_milestone_stages(items: list[MilestoneStageConfig]) -> str:
    return json.dumps([item.model_dump() for item in items])


def row_to_config(row: ProposalSettings) -> WriterConfig:
    return WriterConfig(
        opening_hook=row.opening_hook or "",
        enforce_opening_hook=bool(row.enforce_opening_hook),
        tone=row.tone or "consultative",
        letter_structure=row.letter_structure or DEFAULT_LETTER_STRUCTURE,
        role_letter_structure=row.role_letter_structure or DEFAULT_ROLE_LETTER_STRUCTURE,
        must_include=row.must_include or "",
        never_say=row.never_say or "",
        extra_instructions=row.extra_instructions or "",
        target_words=row.target_words,
        milestone_instructions=row.milestone_instructions or DEFAULT_MILESTONE_INSTRUCTIONS,
        milestone_stages=parse_milestone_stages(row.milestone_stages),
        milestone_min=row.milestone_min or 3,
        milestone_max=row.milestone_max or 5,
        screening_instructions=row.screening_instructions or DEFAULT_SCREENING_INSTRUCTIONS,
        apply_questions_instructions=row.apply_questions_instructions or DEFAULT_APPLY_INSTRUCTIONS,
        example_count=max(0, row.example_count or 0),
    )


def apply_config_to_row(row: ProposalSettings, config: WriterConfig) -> None:
    row.opening_hook = config.opening_hook
    row.enforce_opening_hook = config.enforce_opening_hook
    row.tone = config.tone
    row.letter_structure = config.letter_structure
    row.role_letter_structure = config.role_letter_structure
    row.must_include = config.must_include
    row.never_say = config.never_say
    row.extra_instructions = config.extra_instructions
    row.target_words = config.target_words
    row.milestone_instructions = config.milestone_instructions
    row.milestone_stages = dump_milestone_stages(config.milestone_stages)
    row.milestone_min = config.milestone_min
    row.milestone_max = config.milestone_max
    row.screening_instructions = config.screening_instructions
    row.apply_questions_instructions = config.apply_questions_instructions
    row.example_count = config.example_count


def get_or_create_proposal_settings(db: Session) -> ProposalSettings:
    row = db.query(ProposalSettings).order_by(ProposalSettings.id.asc()).first()
    if row is None:
        row = ProposalSettings()
        apply_config_to_row(row, default_writer_config())
        db.add(row)
        db.flush()
    elif not (row.role_letter_structure or "").strip():
        row.role_letter_structure = DEFAULT_ROLE_LETTER_STRUCTURE
        db.flush()
    return row


def load_proposal_settings(db: Session | None = None) -> WriterConfig:
    own = db is None
    session = db or SessionLocal()
    try:
        row = get_or_create_proposal_settings(session)
        if own:
            session.commit()
        return row_to_config(row)
    finally:
        if own:
            session.close()


def reset_proposal_settings(db: Session) -> WriterConfig:
    row = get_or_create_proposal_settings(db)
    apply_config_to_row(row, default_writer_config())
    db.flush()
    return row_to_config(row)


def reset_role_letter_structure(db: Session) -> WriterConfig:
    row = get_or_create_proposal_settings(db)
    row.role_letter_structure = DEFAULT_ROLE_LETTER_STRUCTURE
    db.flush()
    return row_to_config(row)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def style_rules_for_prompt(db: Session) -> list[str]:
    rows = (
        db.query(PreferenceRule)
        .filter(PreferenceRule.active.is_(True), PreferenceRule.category == "proposal_style")
        .all()
    )
    return [row.rule.strip() for row in rows if row.rule.strip()]


def build_system_prompt(
    config: WriterConfig,
    profile: FreelancerProfile | None = None,
    style_rules: Sequence[str] | None = None,
    engagement: str = "project",
) -> str:
    role_hire = engagement == "role"
    if role_hire:
        goal = (
            "Core goal: they are hiring a person for ongoing work, not buying a one-off build. "
            "Show you understood the seat, how you work with a team and with AI, and one real proof. "
            "Do not invent a product architecture, delivery plan, or escrow stages."
        )
        mix = "Mix roughly 40% role-read + how you work, 40% one real proof, 20% trial/start/CTA."
        structure_text = config.role_letter_structure or DEFAULT_ROLE_LETTER_STRUCTURE
    else:
        goal = (
            "Core goal: make the client think this person understands the problem, knows the difficult part, "
            "already has a plan, and is worth a conversation. Sound like a technical expert presenting a solution, "
            "not a freelancer asking for a job."
        )
        mix = "Mix roughly 70% client problem + how you would build it, 20% proof, 10% you + availability + CTA."
        structure_text = config.letter_structure or DEFAULT_LETTER_STRUCTURE
    parts: list[str] = [
        "You write Upwork proposals for a technical freelancer.",
        "",
        goal,
        "",
        f"Engagement type: {engagement}.",
    ]
    if role_hire:
        parts.extend(
            [
                "",
                "Do not write as if they posted a scoped build. Do not say you would design their platform this week. "
                "Do not name a fake hardest-part of an architecture they did not ask you to deliver.",
            ]
        )
    if profile and profile.voice.strip():
        parts.extend(["", f"Voice: {profile.voice.strip()}"])
    parts.extend(["", f"Tone: {config.tone}."])
    if config.enforce_opening_hook and config.opening_hook.strip():
        parts.extend(
            [
                "",
                "Opening hook: the cover_letter MUST start with this exact sentence, then a blank line:",
                config.opening_hook.strip(),
                "Do not paraphrase it. Do not skip it. Do not add any line before it. No other greetings besides this hook.",
            ]
        )
    elif config.opening_hook.strip():
        parts.extend(["", "Suggested opening (optional):", config.opening_hook.strip()])
    structure = _lines(structure_text)
    if structure:
        parts.extend(["", "Formula (use this order):"])
        for index, line in enumerate(structure, 1):
            parts.append(f"{index}. {line}")
    must = _lines(config.must_include)
    if must:
        parts.extend(["", "Always include:"])
        parts.extend(f"- {item}" for item in must)
    never = _lines(config.never_say)
    if never:
        parts.extend(["", "Forbidden filler / never say:"])
        parts.extend(f"- {item}" for item in never)
    if config.apply_questions_instructions.strip():
        parts.extend(["", "Posting apply items:", config.apply_questions_instructions.strip()])
    if config.screening_instructions.strip():
        parts.extend(["", "Screening questions:", config.screening_instructions.strip()])
    if config.milestone_instructions.strip() and not role_hire:
        parts.extend(["", "Milestones:", config.milestone_instructions.strip()])
    if config.target_words:
        parts.extend(["", f"Aim for about {config.target_words} words in the cover letter."])
    rules = [item.strip() for item in (style_rules or []) if item.strip()]
    if rules:
        parts.extend(["", "Proposal style rules from Settings:"])
        parts.extend(f"- {item}" for item in rules)
    if config.extra_instructions.strip():
        parts.extend(["", "Extra instructions:", config.extra_instructions.strip()])
    parts.extend(
        [
            "",
            mix,
            "",
            JSON_OUTPUT_RULES,
        ]
    )
    return "\n".join(parts)


def select_examples(db: Session, job_blob: str, limit: int) -> list[StyleExample]:
    if limit <= 0:
        return []
    rows = db.query(ProposalExample).filter(ProposalExample.active.is_(True)).all()
    usable = [row for row in rows if (row.job_post or "").strip() and (row.cover_letter or "").strip()]
    if not usable:
        return []
    texts = [job_blob or ""] + [row.job_post for row in usable]
    vectors = embed_texts(texts)
    query = vectors[0]
    scored: list[tuple[float, ProposalExample]] = []
    for row, vector in zip(usable, vectors[1:], strict=True):
        scored.append((cosine(query, vector), row))
    scored.sort(key=lambda item: item[0], reverse=True)
    picked: list[StyleExample] = []
    for _score, row in scored[:limit]:
        picked.append(
            {
                "title": row.title,
                "job_post": row.job_post,
                "cover_letter": row.cover_letter,
                "notes": row.notes or "",
            }
        )
    return picked
