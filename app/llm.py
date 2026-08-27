import json
from typing import Any, TypedDict

from openai import BadRequestError, OpenAI
from pydantic import ValidationError

from app.config import Settings
from app.milestones import coerce_milestones
from app.models import (
    DraftResult,
    FreelancerProfile,
    JobPayload,
    ReplyIntent,
    ReplyIntentKind,
    ScoreResult,
    ScreeningAnswer,
    SearchQueryContext,
    StyleExample,
    SuggestReplyResult,
    display_intent_label,
)
from app.proposal_writer import (
    SYSTEM_PROMPT,
    extract_apply_questions,
    finalize_letter,
    job_context,
    parse_screening_answers,
    profile_for_prompt,
    sanitize_proposal,
)


class ChatMessage(TypedDict):
    role: str
    content: str


def _client(settings: Settings) -> OpenAI:
    kwargs: dict[str, object] = {"api_key": settings.openai_api_key}
    base = (settings.openai_base_url or "").strip()
    kwargs["base_url"] = base or "https://api.openai.com/v1"
    return OpenAI(**kwargs)


def _require_key(settings: Settings) -> None:
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI API key is required to draft proposals")


def _temperature_supported(model: str) -> bool:
    name = model.strip().lower()
    return not name.startswith(("gpt-5", "o1", "o3", "o4"))


def _complete_json(
    client: OpenAI,
    model: str,
    messages: list[ChatMessage],
    temperature: float,
) -> dict[str, Any]:
    kwargs: dict[str, object] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    try:
        if _temperature_supported(model):
            response = client.chat.completions.create(**kwargs, temperature=temperature)
        else:
            response = client.chat.completions.create(**kwargs)
    except BadRequestError as exc:
        if "temperature" not in str(exc).lower():
            raise
        response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content or "{}"
    parsed = json.loads(content)
    return parsed if isinstance(parsed, dict) else {}


def draft_model(settings: Settings) -> str:
    value = (settings.openai_draft_model or "").strip()
    return value or settings.openai_model


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
    parsed = ScoreResult.model_validate(
        _complete_json(
            client,
            settings.openai_model,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            0.2,
        )
    )
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
    system_prompt: str = "",
    style_examples: list[StyleExample] | None = None,
    milestone_min: int = 3,
    milestone_max: int = 5,
    apply_questions_instructions: str = "",
    screening_instructions: str = "",
    opening_hook: str | None = None,
    enforce_hook: bool = True,
) -> DraftResult:
    _require_key(settings)
    client = _client(settings)
    questions = [item for item in (screening_questions or []) if item.strip()]
    apply_items = extract_apply_questions(str(job.get("description") or ""))
    low = max(1, milestone_min)
    high = max(low, milestone_max)
    milestone_rule = ""
    if milestones_budget:
        milestone_rule = (
            f" This is a fixed-price job. Return milestones as an array of objects with keys "
            f"title, description, amount (number). Use {low}-{high} stages that sum to {milestones_budget:g}. "
            "Do not mention those milestones, stage lists, or dollar amounts in cover_letter."
        )
    system = (system_prompt or SYSTEM_PROMPT) + f"\nTone: {tone}." + milestone_rule
    proof_block = ""
    if proof.strip():
        proof_block = (
            "\n\nONE completed project/contract from YOUR work history. Cite only this. "
            "Never claim you worked a job posting you applied to. "
            "Do not invent contracts, metrics, clients, or employers:\n"
            + proof.strip()
        )
    elif examples:
        proof_block = (
            "\n\nONE completed project from this context. Cite only what is here. "
            "Never treat a job posting as work you did:\n" + "\n---\n".join(examples[:1])
        )
    focus_block = ""
    if focus_points:
        focus_block = "\nFocus points:\n- " + "\n- ".join(focus_points)
    apply_block = ""
    if apply_items:
        numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(apply_items, 1))
        hint = apply_questions_instructions.strip() or (
            "Answer each as a labeled section in cover_letter. "
            "Do not put them in screening_answers. Do not invent GitHub URLs or demos."
        )
        apply_block = f"\n\nPosting 'please include' items. {hint}\n" + numbered
    screening_block = ""
    if questions:
        extra = screening_instructions.strip()
        screening_block = "\n\nOfficial Upwork screening questions to answer in screening_answers (not in the letter):\n- " + "\n- ".join(questions)
        if extra:
            screening_block += f"\n{extra}"
    style_block = ""
    if style_examples:
        chunks: list[str] = []
        for item in style_examples:
            chunks.append(
                f"Title: {item.get('title') or 'example'}\n"
                f"Job post:\n{(item.get('job_post') or '')[:2500]}\n\n"
                f"Cover letter they would write:\n{(item.get('cover_letter') or '')[:2500]}"
            )
        style_block = (
            "\n\nSTYLE EXAMPLES (technique only). Do not copy them, paraphrase them, or reuse their domain, "
            "client, claims, metrics, or GitHub URLs. Write a version that fits THIS job.\n\n"
            + "\n\n---\n\n".join(chunks)
        )
    hours = profile.working_hours.strip()
    hours_block = f"\nYour working hours (use in the CTA with the job timezone; do not invent hours): {hours}" if hours else ""
    user = (
        f"Profile:\n{json.dumps(profile_for_prompt(profile), ensure_ascii=False, indent=2)}\n"
        f"{hours_block}\n\n"
        f"Job:\n{json.dumps(job_context(job), ensure_ascii=False, indent=2)}"
        f"{proof_block}{focus_block}{apply_block}{screening_block}{style_block}"
    )
    parsed = _complete_json(
        client,
        draft_model(settings),
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        0.4,
    )
    letter = finalize_letter(str(parsed.get("cover_letter") or ""), hook=opening_hook, enforce=enforce_hook)
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
    screening_instructions: str = "",
    proof: str = "",
) -> list[ScreeningAnswer]:
    cleaned = [item.strip() for item in questions if item.strip()]
    if not cleaned:
        return []
    _require_key(settings)
    client = _client(settings)
    extra = screening_instructions.strip() or "Be specific to this job. No buzzwords. No em dashes."
    numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(cleaned, 1))
    proof_block = ""
    if proof.strip():
        proof_block = (
            "Completed work you may cite. Do not cite job postings or applications:\n"
            + proof.strip()
            + "\n\n"
        )
    user = (
        f"Profile:\n{json.dumps(profile_for_prompt(profile), ensure_ascii=False, indent=2)}\n\n"
        f"Job:\n{json.dumps(job_context(job), ensure_ascii=False, indent=2)}\n\n"
        f"{proof_block}"
        f"Cover letter already written:\n{letter[:4000]}\n\n"
        f"Official Upwork screening questions:\n{numbered}\n\n"
        "Answer every question in JSON {\"screening_answers\": [{\"question\", \"answer\"}]}. "
        "Copy each question text exactly. Do not return an empty array. "
        "Only cite completed contracts, portfolio projects, or employment. "
        "Never claim you worked a job you only applied to. "
        + extra
    )
    parsed = _complete_json(
        client,
        draft_model(settings),
        [
            {
                "role": "system",
                "content": "Answer Upwork screening questions for a technical freelancer. Return JSON only.",
            },
            {"role": "user", "content": user},
        ],
        0.3,
    )
    answers = parse_screening_answers(parsed.get("screening_answers"), cleaned)
    filled = [
        ScreeningAnswer(question=item.question, answer=sanitize_proposal(item.answer))
        for item in answers
    ]
    if any(not item.answer.strip() for item in filled):
        raise RuntimeError("LLM returned empty screening answers")
    return filled


def llm_suggest_search_queries(context: SearchQueryContext, settings: Settings) -> list[str]:
    _require_key(settings)
    client = _client(settings)
    system = (
        "You propose Upwork job-search keyword queries for one freelancer. "
        "Return JSON only with key queries: an array of 6 to 10 short strings. "
        "Each query is 3 to 7 keywords like 'LLM RAG LangGraph production'. "
        "Use only skills and work that appear in the profile or history. "
        "Never suggest excluded keywords. Do not repeat current_queries. "
        "Prefer specific stacks over generic titles like AI engineer."
    )
    user = json.dumps(context, ensure_ascii=False, indent=2)
    parsed = _complete_json(
        client,
        settings.openai_model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        0.3,
    )
    raw = parsed.get("queries")
    if not isinstance(raw, list):
        raw = parsed.get("search_queries")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= 10:
            break
    return out


def clamp_reply_intents(result: SuggestReplyResult) -> list[ReplyIntent]:
    seen: set[ReplyIntentKind] = set()
    out: list[ReplyIntent] = []
    for item in result.intents:
        text = (item.text or "").strip()
        if not text or item.kind in seen:
            continue
        label = display_intent_label(item.kind, item.label)
        out.append(ReplyIntent(kind=item.kind, label=label[:48], text=text))
        seen.add(item.kind)
        if len(out) >= 3:
            break
    if not out:
        fallback = (result.text or "").strip()
        if fallback:
            out.append(
                ReplyIntent(
                    kind=ReplyIntentKind.general_answer,
                    label=display_intent_label(ReplyIntentKind.general_answer),
                    text=fallback,
                )
            )
    return out


def parse_suggest_reply(content: str) -> SuggestReplyResult:
    parsed = json.loads(content or "{}")
    if not isinstance(parsed, dict):
        parsed = {}
    raw_intents = parsed.get("intents")
    if not isinstance(raw_intents, list):
        raw_intents = []
    collected: list[ReplyIntent] = []
    for item in raw_intents:
        if not isinstance(item, dict):
            continue
        try:
            collected.append(ReplyIntent.model_validate(item))
        except ValidationError:
            continue
    leftover = str(parsed.get("text") or parsed.get("reply") or "").strip()
    intents = clamp_reply_intents(SuggestReplyResult(intents=collected, text=leftover))
    return SuggestReplyResult(intents=intents, text=leftover)


def llm_suggest_reply(
    transcript: str,
    profile: FreelancerProfile,
    settings: Settings,
    counterpart: str = "",
) -> SuggestReplyResult:
    _require_key(settings)
    client = _client(settings)
    system = (
        "You draft short Upwork chat replies for a freelancer. "
        "Return JSON only with key intents: 2 or 3 objects, each with kind, label, text. "
        "kind must be follow_up, set_meeting, or general_answer. Pick only kinds that fit the thread. "
        "label is a short human phrase such as Set a meeting, never snake_case. "
        "follow_up: nudge if the conversation stalled. "
        "set_meeting: they asked for a call or time; ask them to suggest times, never invent availability. "
        "general_answer: a direct reply to the latest client message. "
        "Consultative, specific, no fake claims, no invented rates. "
        "Plain text replies. No markdown headings. No em dashes."
    )
    user = (
        f"Freelancer profile:\n{json.dumps(profile_for_prompt(profile), ensure_ascii=False, indent=2)}\n\n"
        f"Chat with: {counterpart or 'the client'}\n\n"
        f"Transcript (oldest first):\n{transcript[:12000]}\n\n"
        "Return {\"intents\": [{\"kind\", \"label\", \"text\"}, ...]}."
    )
    parsed = _complete_json(
        client,
        settings.openai_model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        0.4,
    )
    result = parse_suggest_reply(json.dumps(parsed))
    if not result.intents:
        raise RuntimeError("LLM returned no reply intents")
    return result
