import json

from openai import OpenAI

from app.config import Settings
from app.milestones import coerce_milestones
from app.models import DraftResult, FreelancerProfile, JobPayload, ScoreResult, ScreeningAnswer
from app.proposal_writer import (
    SYSTEM_PROMPT,
    extract_apply_questions,
    finalize_letter,
    job_context,
    parse_screening_answers,
    profile_for_prompt,
    sanitize_proposal,
)


def _client(settings: Settings) -> OpenAI:
    kwargs: dict[str, object] = {"api_key": settings.openai_api_key}
    base = (settings.openai_base_url or "").strip()
    kwargs["base_url"] = base or "https://api.openai.com/v1"
    return OpenAI(**kwargs)


def _require_key(settings: Settings) -> None:
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI API key is required to draft proposals")


def llm_score(job: JobPayload, profile: FreelancerProfile, settings: Settings) -> ScoreResult:
    client = _client(settings)
    system = (
        "You score Upwork jobs for a freelancer. Return JSON only with keys "
        "score (0-100 integer), reason (short), should_apply (boolean). "
        "Penalize mismatch, unpaid tests, and excluded keywords."
    )
    user = (
        f"Profile:\n{profile.model_dump_json(indent=2)}\n\n"
        f"Job:\n{json.dumps(job_context(job), ensure_ascii=False, indent=2)}"
    )
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    content = response.choices[0].message.content or "{}"
    parsed = ScoreResult.model_validate_json(content)
    parsed.go = parsed.should_apply
    return parsed


def llm_draft(
    job: JobPayload,
    profile: FreelancerProfile,
    settings: Settings,
    examples: list[str] | None = None,
    tone: str = "consultative",
    focus_points: list[str] | None = None,
    milestones_budget: float | None = None,
    proof: str = "",
    screening_questions: list[str] | None = None,
) -> DraftResult:
    _require_key(settings)
    client = _client(settings)
    questions = [item for item in (screening_questions or []) if item.strip()]
    apply_items = extract_apply_questions(str(job.get("description") or ""))
    milestone_rule = ""
    if milestones_budget:
        milestone_rule = (
            f" This is a fixed-price job. Return milestones as an array of objects with keys "
            f"title, description, amount (number). Use 3-5 stages that sum to {milestones_budget:g}. "
            "Do not mention those milestones, stage lists, or dollar amounts in cover_letter."
        )
    system = SYSTEM_PROMPT + f"\nTone: {tone}." + milestone_rule
    proof_block = ""
    if proof.strip():
        proof_block = (
            "\n\nONE relevant project. Cite this as evidence. Do not invent contracts, metrics, or clients:\n"
            + proof.strip()
        )
    elif examples:
        proof_block = (
            "\n\nONE relevant project from this context. Cite only what is here:\n" + "\n---\n".join(examples[:1])
        )
    focus_block = ""
    if focus_points:
        focus_block = "\nFocus points:\n- " + "\n- ".join(focus_points)
    apply_block = ""
    if apply_items:
        numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(apply_items, 1))
        apply_block = (
            "\n\nPosting 'please include' items. Answer each as a labeled section in cover_letter. "
            "Do not put them in screening_answers. Do not invent GitHub URLs or demos:\n"
            + numbered
        )
    screening_block = ""
    if questions:
        screening_block = "\n\nOfficial Upwork screening questions to answer in screening_answers (not in the letter):\n- " + "\n- ".join(questions)
    hours = profile.working_hours.strip()
    hours_block = f"\nYour working hours (use in the CTA with the job timezone; do not invent hours): {hours}" if hours else ""
    user = (
        f"Profile:\n{json.dumps(profile_for_prompt(profile), ensure_ascii=False, indent=2)}\n"
        f"{hours_block}\n\n"
        f"Job:\n{json.dumps(job_context(job), ensure_ascii=False, indent=2)}"
        f"{proof_block}{focus_block}{apply_block}{screening_block}"
    )
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.4,
    )
    content = response.choices[0].message.content or "{}"
    parsed = json.loads(content)
    letter = finalize_letter(str(parsed.get("cover_letter") or ""))
    if not letter:
        raise RuntimeError("LLM returned an empty cover letter")
    planned = coerce_milestones(parsed.get("milestones"), milestones_budget) if milestones_budget else []
    answers = parse_screening_answers(parsed.get("screening_answers"), questions)
    return DraftResult(cover_letter=letter, milestones=planned, screening_answers=answers)


def llm_screening_answers(
    job: JobPayload,
    profile: FreelancerProfile,
    settings: Settings,
    letter: str,
    questions: list[str],
) -> list[ScreeningAnswer]:
    cleaned = [item.strip() for item in questions if item.strip()]
    if not cleaned:
        return []
    _require_key(settings)
    client = _client(settings)
    user = (
        f"Profile:\n{json.dumps(profile_for_prompt(profile), ensure_ascii=False, indent=2)}\n\n"
        f"Job:\n{json.dumps(job_context(job), ensure_ascii=False, indent=2)}\n\n"
        f"Cover letter already written:\n{letter[:4000]}\n\n"
        "Answer each screening question in JSON {\"screening_answers\": [{\"question\", \"answer\"}]}. "
        "Be specific to this job. No buzzwords. No em dashes."
    )
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {
                "role": "system",
                "content": "Answer Upwork screening questions for a technical freelancer. Return JSON only.",
            },
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    content = response.choices[0].message.content or "{}"
    parsed = json.loads(content)
    answers = parse_screening_answers(parsed.get("screening_answers"), cleaned)
    return [
        ScreeningAnswer(question=item.question, answer=sanitize_proposal(item.answer))
        for item in answers
    ]
