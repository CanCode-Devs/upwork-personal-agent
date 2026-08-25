from __future__ import annotations

import json
import re
from typing import Any

from app.db.models import Proposal
from app.models import FreelancerProfile, JobPayload, ScreeningAnswer

OPENING_HOOK = (
    "Hey, you'll probably get a lot of AI-generated proposals, so I'll try to write this one manually."
)

SYSTEM_PROMPT = """You write Upwork proposals for a technical freelancer.

Core goal: make the client think this person understands the problem, knows the difficult part, already has a plan, and is worth a conversation. Sound like a technical expert presenting a solution, not a freelancer asking for a job.

Formula (use this order):
1. Opening hook: the cover_letter MUST start with this exact sentence, then a blank line:
Hey, you'll probably get a lot of AI-generated proposals, so I'll try to write this one manually.
Do not paraphrase it. Do not skip it. Do not add any line before it.
2. Technical hook: 1-2 lines on what you would actually build for THIS job.
3. Hardest part: the risk or engineering effort beyond the job-post happy path.
4. Solution: 1-3 sentences on the approach.
5. Relevant work: ONE project from the provided proof. Do not invent contracts, metrics, or clients.
6. Posting apply items: if job.apply_questions is non-empty, add one labeled section per item AFTER relevant work and BEFORE the offer/CTA. Use the item text as the heading. Answer with real proof from the provided profile/proof only. Never invent GitHub URLs, repos, demos, case studies, or metrics. If you cannot prove an item, say so in one sentence and name the closest real work. These answers belong in cover_letter, not in screening_answers.
7. Offer: a low-risk reason to start talking, only when the job is substantial.
8. Direct CTA: tell them what to send or do next. If a client timezone and working hours are provided, make availability specific to those. Do not invent hours.
If job.attachment_text is present, use those requirements in the technical hook. Do not quote long passages.
If job.client_first_name is provided, you may use it in the CTA. Never invent a name.
Never put escrow milestones, numbered delivery stages, or dollar amounts for stages in the cover letter. Upwork has a dedicated milestones form; return those only in the milestones JSON field.

Mix roughly 70% client problem + how you would build it, 20% proof, 10% you + availability + CTA.

Style:
- Short scannable sections. Short sentences.
- Separate every section with a blank line. The cover_letter MUST contain real paragraph breaks (newline + newline). Never return a single wall of text.
- No em dashes. No decorative quotation marks around ordinary phrases.
- No other greetings besides the required opening hook (no Hope you are well, no extra Hi).
- Forbidden filler: excited to apply, would love to work, perfect candidate, great fit, extensive experience, looking forward to hearing, passionate about, proven track record, cutting-edge, revolutionary, seamless, robust, next-generation, innovative, world-class.
- Never paste the client's job description back at them.

The following are EXAMPLES OF TECHNIQUE ONLY. Do not copy them, paraphrase them, or reuse their domain (CRM, n8n, appointment booking, OpenAI-to-CRM). Write a version that fits THIS job. The required opening hook above is the one exception: copy it exactly.

EXAMPLE (do not copy) technical hook technique: name the actual architecture (trigger, agent, where qualified work goes) instead of "I can build your AI system".
EXAMPLE (do not copy) hardest-part technique: name the failure mode (state, duplicates, async, messy data) rather than the easy integration.
EXAMPLE (do not copy) CTA technique: ask for a concrete artifact (current workflow, sample, architecture) and a short call, tied to their timezone.

Return JSON only with keys:
- cover_letter (string)
- milestones (array of {title, description, amount} summing to the given bid when milestones are requested; otherwise [])
- screening_answers (array of {question, answer} for each provided screening question; otherwise [])
"""


def job_context(job: JobPayload) -> dict[str, Any]:
    details: dict[str, Any] = {}
    raw = job.get("client_details") or job.get("client") or "{}"
    if isinstance(raw, dict):
        details = raw
    else:
        try:
            parsed = json.loads(str(raw) or "{}")
            if isinstance(parsed, dict):
                details = parsed
        except json.JSONDecodeError:
            details = {}
    location_parts = [details.get("city"), details.get("state"), details.get("country")]
    location = ", ".join(str(part) for part in location_parts if part)
    reviews = details.get("client_reviews") if isinstance(details.get("client_reviews"), list) else []
    review_bits: list[str] = []
    first_name = str(details.get("client_first_name") or "").strip()
    for row in reviews[:5]:
        if not isinstance(row, dict):
            continue
        reviewer = str(row.get("reviewer") or "").strip()
        snippet = " · ".join(
            part
            for part in (
                reviewer,
                f"{row.get('rating')}★" if row.get("rating") not in (None, "") else "",
                str(row.get("title") or "").strip(),
                str(row.get("comment") or "").strip()[:180],
            )
            if part
        )
        if snippet:
            review_bits.append(snippet)
    attachment_names: list[str] = []
    for item in details.get("attachments") or []:
        if isinstance(item, dict) and item.get("filename"):
            attachment_names.append(str(item["filename"]))
    return {
        "title": job.get("title") or "",
        "description": (job.get("description") or "")[:8000],
        "budget": job.get("budget") or job.get("price_label") or "",
        "job_type": job.get("job_type") or "",
        "timezone": job.get("timezone") or str(details.get("timezone") or ""),
        "location": location,
        "duration": str(details.get("duration") or job.get("estimated_duration") or ""),
        "client_name": str(details.get("company") or "").strip(),
        "client_first_name": first_name,
        "member_since": str(details.get("member_since") or ""),
        "hire_rate": details.get("hire_rate"),
        "spend_total": details.get("spend_total"),
        "avg_spend": details.get("avg_spend"),
        "invites_sent": details.get("invites_sent"),
        "interviewing": details.get("interviewing"),
        "client_reviews": review_bits,
        "attachment_names": attachment_names,
        "attachment_text": str(details.get("attachment_text") or "")[:8000],
        "apply_questions": extract_apply_questions(str(job.get("description") or "")),
    }


_APPLY_HEADING = re.compile(
    r"(?im)^[ \t]*(?:to apply|please include(?: in your (?:proposal|application))?|your proposal (?:must|should) include)\b[^\n]*$"
)
_APPLY_ITEM = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])\s+(.+)$")


def extract_apply_questions(description: str) -> list[str]:
    text = (description or "").replace("\r\n", "\n")
    matches = list(_APPLY_HEADING.finditer(text))
    if not matches:
        return []
    best: list[str] = []
    for match in matches:
        collected: list[str] = []
        started = False
        for line in text[match.end() :].split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if _APPLY_HEADING.match(stripped):
                continue
            item = _APPLY_ITEM.match(stripped)
            if item:
                started = True
                collected.append(re.sub(r"\s+", " ", item.group(1)).strip())
                continue
            if started:
                break
        if len(collected) > len(best):
            best = collected
    seen: set[str] = set()
    items: list[str] = []
    for item in best:
        key = item.lower()
        if key in seen or len(item) < 8:
            continue
        seen.add(key)
        items.append(item)
    return items[:12]


def sanitize_proposal(text: str) -> str:
    cleaned = text.replace("\u2014", ", ").replace("\u2013", "-").replace(" -- ", ", ")
    if "\\n" in cleaned and "\n" not in cleaned:
        cleaned = cleaned.replace("\\n", "\n")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" ,", ",", cleaned)
    return ensure_paragraphs(strip_milestone_section(cleaned.strip()))


def finalize_letter(text: str) -> str:
    return ensure_opening_hook(sanitize_proposal(text))


def ensure_paragraphs(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    if "\n\n" in raw:
        parts = [part.strip() for part in re.split(r"\n{2,}", raw) if part.strip()]
        return "\n\n".join(parts)
    if raw.count("\n") >= 2:
        parts = [part.strip() for part in raw.split("\n") if part.strip()]
        return "\n\n".join(parts)
    numbered = re.search(r"\s\d+\.\s", raw)
    body = raw
    tail = ""
    if numbered:
        body = raw[: numbered.start()].strip()
        tail = raw[numbered.start() :].strip()
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", body) if part.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    for sentence in sentences:
        buf.append(sentence)
        if len(buf) >= 2:
            chunks.append(" ".join(buf))
            buf = []
    if buf:
        chunks.append(" ".join(buf))
    if tail:
        stages = [part.strip() for part in re.split(r"(?=\d+\.\s)", tail) if part.strip()]
        if stages:
            chunks.append("\n".join(stages))
    return "\n\n".join(chunks) if chunks else raw


_MILESTONE_HEAD = re.compile(
    r"(?:\n[ \t]*)+(?:Proposed milestones|Escrow milestones|Milestone plan)\s*:?\s*\n[\s\S]*\Z",
    re.IGNORECASE,
)


def strip_milestone_section(letter: str) -> str:
    cleaned = (letter or "").replace("\r\n", "\n")
    cleaned = _MILESTONE_HEAD.sub("", cleaned)
    return cleaned.strip()


_AI_HOOK = re.compile(
    r"^(hey[,.]?\s+)?you(?:'ll| will) probably (?:get|receive) a lot of ai[- ]generated proposals?\b[^.]*\.\s*",
    re.IGNORECASE | re.DOTALL,
)


def ensure_opening_hook(letter: str) -> str:
    body = (letter or "").strip()
    if not body:
        return OPENING_HOOK
    body = _AI_HOOK.sub("", body).lstrip()
    if body.lower().startswith(OPENING_HOOK.lower()):
        body = body[len(OPENING_HOOK) :].lstrip()
    if not body:
        return OPENING_HOOK
    return f"{OPENING_HOOK}\n\n{body}"


def letter_has_plan(letter: str) -> bool:
    lower = letter.lower()
    return any(
        token in lower
        for token in (
            "milestone 1",
            "proposed milestones",
            "\nimplementation\n",
            "implementation:",
        )
    )


def clean_screening_question(text: str) -> str:
    stripped = re.sub(r"</?untrusted_participant_content>", "", text, flags=re.I)
    return stripped.strip()


def parse_screening_answers(raw: Any, questions: list[str]) -> list[ScreeningAnswer]:
    by_question: dict[str, str] = {}
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            question = clean_screening_question(str(row.get("question") or ""))
            answer = str(row.get("answer") or "").strip()
            if question and answer:
                by_question[question] = answer
    leftover: list[str] = []
    if isinstance(raw, list):
        leftover = [str(row.get("answer") or "").strip() for row in raw if isinstance(row, dict)]
    items: list[ScreeningAnswer] = []
    for index, question in enumerate(questions):
        cleaned = clean_screening_question(question)
        if not cleaned:
            continue
        answer = by_question.get(cleaned, "")
        if not answer and index < len(leftover):
            answer = leftover[index]
        items.append(ScreeningAnswer(question=cleaned, answer=answer))
    return items


def profile_for_prompt(profile: FreelancerProfile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "title": profile.title,
        "skills": profile.skills,
        "voice": profile.voice,
        "working_hours": profile.working_hours,
        "overview": (profile.upwork_overview or "")[:1500],
    }


def dump_screening(items: list[ScreeningAnswer]) -> str:
    return json.dumps([item.model_dump() for item in items], ensure_ascii=False)


def load_screening(proposal: Proposal | None) -> list[ScreeningAnswer]:
    if proposal is None:
        return []
    raw = getattr(proposal, "screening_json", "") or "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[ScreeningAnswer] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        question = clean_screening_question(str(row.get("question") or ""))
        if not question:
            continue
        items.append(ScreeningAnswer(question=question, answer=str(row.get("answer") or "").strip()))
    return items


def dump_apply(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def load_apply(proposal: Proposal | None) -> dict[str, Any]:
    if proposal is None:
        return {}
    raw = getattr(proposal, "apply_json", "") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_screening_form(questions: list[str], answers: list[str]) -> list[ScreeningAnswer]:
    count = max(len(questions), len(answers))
    items: list[ScreeningAnswer] = []
    for index in range(count):
        question = clean_screening_question(questions[index] if index < len(questions) else "")
        if not question:
            continue
        answer = answers[index].strip() if index < len(answers) else ""
        items.append(ScreeningAnswer(question=question, answer=answer))
    return items
