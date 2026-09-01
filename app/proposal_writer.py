from __future__ import annotations

import json
import re
from typing import Any, Sequence

from app.db.models import Proposal
from app.engagement import classify_engagement
from app.models import CritiqueResult, ScreeningAnswer, UnprovenGap

OPENING_HOOK = ""

SYSTEM_PROMPT = """You write Upwork proposals for a technical freelancer.

Core goal: make the client think this person understands the problem, knows the difficult part, already has a plan, and is worth a conversation. Sound like a technical expert presenting a solution, not a freelancer asking for a job.

Formula (use this order):
1. Technical hook: 1-2 lines on what you would actually build for THIS job.
2. Hardest part: the risk or engineering effort beyond the job-post happy path.
3. Solution: 1-3 sentences on the approach.
4. Relevant work: ONE project from the provided proof. Proof is completed work only (contract, portfolio project, or employment). Never treat another Upwork job posting, application, or proposal as work you did. Do not invent contracts, metrics, clients, or employers. If proof is empty, cite profile skills only and do not name a client.
5. Posting apply items: if job.apply_questions is non-empty, add one labeled section per item AFTER relevant work and BEFORE the offer/CTA.
6. Offer: a low-risk reason to start talking, only when the job is substantial.
7. Direct CTA: tell them what to send or do next.

Mix roughly 70% client problem + how you would build it, 20% proof, 10% you + availability + CTA.

Style:
- Short scannable sections. Short sentences.
- Separate every section with a blank line.
- No em dashes. No decorative quotation marks around ordinary phrases.
- Forbidden filler: excited to apply, would love to work, perfect candidate, great fit, extensive experience, looking forward to hearing, passionate about, proven track record, cutting-edge, revolutionary, seamless, robust, next-generation, innovative, world-class.
- Never paste the client's job description back at them.

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
    closed_titles: list[str] = []
    closed = details.get("closed_contracts") if isinstance(details.get("closed_contracts"), list) else []
    for row in closed[:6]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if title:
            closed_titles.append(title)
    description = (job.get("description") or "")[:8000]
    attachment_text = str(details.get("attachment_text") or "")[:8000]
    return {
        "title": job.get("title") or "",
        "description": description,
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
        "closed_contracts": closed_titles,
        "attachment_names": attachment_names,
        "attachment_text": attachment_text,
        "posting_mentions_attachments": posting_mentions_attachments(str(job.get("description") or "")),
        "apply_questions": extract_apply_questions(str(job.get("description") or "")),
        "engagement": classify_engagement(
            str(job.get("title") or ""),
            str(job.get("description") or ""),
            str(job.get("job_type") or ""),
        ),
    }


_APPLY_HEADING = re.compile(
    r"(?im)^[ \t]*(?:to apply|please include(?: in your (?:proposal|application))?|your proposal (?:must|should) include)\b[^\n]*$"
)
_APPLY_ITEM = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])\s+(.+)$")
_ATTACHMENT_MENTION = re.compile(
    r"\b(?:attach(?:ed|ment|ments)?|screenshots?)\b",
    re.IGNORECASE,
)


def posting_mentions_attachments(description: str) -> bool:
    return bool(_ATTACHMENT_MENTION.search(description or ""))


def attachments_unreadable(description: str, details: dict[str, Any] | None) -> bool:
    if not posting_mentions_attachments(description):
        return False
    blob = details if isinstance(details, dict) else {}
    return not str(blob.get("attachment_text") or "").strip()


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


UNPROVEN_SOURCE = (
    r"can(?:not|'t)\s+(?:truthfully\s+)?(?:claim|verify|prove)"
    r"|won'?t\s+invent"
    r"|will not invent"
    r"|from the provided (?:proof|profile)"
    r"|closest real work"
    r"|i don'?t have a specific"
    r"|i do not have a specific"
)
UNPROVEN_PATTERN = re.compile(UNPROVEN_SOURCE, re.IGNORECASE)


class UnprovenAnswersError(ValueError):
    def __init__(self, gaps: list[UnprovenGap]) -> None:
        self.gaps = gaps
        super().__init__("Fix unproven answers before submit")


def _fold_quotes(text: str) -> str:
    return (text or "").replace("\u2019", "'").replace("\u2018", "'")


def is_unproven_text(text: str) -> bool:
    return bool(UNPROVEN_PATTERN.search(_fold_quotes(text)))


def _letter_sections(letter: str) -> list[str]:
    text = (letter or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    chunks: list[str] = []
    for part in re.split(r"\n{2,}", text):
        bits = re.split(r"(?=\n?\s*\d+[.)]\s+)", part)
        for bit in bits:
            cleaned = bit.strip()
            if cleaned:
                chunks.append(cleaned)
    return chunks


def _heading_from_section(section: str) -> str:
    first = section.strip().split("\n", 1)[0].strip()
    first = re.sub(r"^\d+[.)]\s*", "", first)
    return first[:120] or "Unproven answer"


def _question_in_letter(letter: str, question: str) -> bool:
    blob = re.sub(r"\s+", " ", letter or "").lower()
    key = re.sub(r"\s+", " ", question or "").strip().lower()
    if len(key) > 60:
        key = key[:60]
    return bool(key) and key in blob


def unproven_answer_gaps(
    letter: str,
    screening: Sequence[ScreeningAnswer] | None = None,
    apply_questions: Sequence[str] | None = None,
) -> list[UnprovenGap]:
    gaps: list[UnprovenGap] = []
    seen: set[str] = set()

    def add(source: str, heading: str, snippet: str) -> None:
        title = (heading or "Unproven answer").strip() or "Unproven answer"
        key = f"{source}:{title.lower()}"
        if key in seen:
            return
        seen.add(key)
        gaps.append({"source": source, "heading": title[:160], "snippet": (snippet or "").strip()[:180]})

    for item in screening or []:
        question = item.question.strip() or "Screening question"
        answer = item.answer or ""
        if not answer.strip():
            add("screening", question, "Empty answer")
        elif is_unproven_text(answer):
            add("screening", question, answer.strip())

    for section in _letter_sections(letter):
        if is_unproven_text(section):
            add("letter", _heading_from_section(section), section)

    folded = letter or ""
    for question in apply_questions or []:
        text = (question or "").strip()
        if text and not _question_in_letter(folded, text):
            add("apply", text, "Not answered in the letter")
    return gaps


def sanitize_proposal(text: str) -> str:
    cleaned = text.replace("\u2014", ", ").replace("\u2013", "-").replace(" -- ", ", ")
    if "\\n" in cleaned and "\n" not in cleaned:
        cleaned = cleaned.replace("\\n", "\n")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" ,", ",", cleaned)
    return ensure_paragraphs(strip_milestone_section(cleaned.strip()))


def finalize_letter(text: str, hook: str | None = None, enforce: bool = True) -> str:
    return ensure_opening_hook(sanitize_proposal(text), hook=hook, enforce=enforce)


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
    r"^(hey[,.]?\s+)?you(?:'ll| will) probably (?:get|receive) a lot of ai[- ]generated proposals?\b[^.]*\.\s*(?:let'?s get to it\.?\s*)?",
    re.IGNORECASE | re.DOTALL,
)


def strip_ai_hook(letter: str) -> str:
    body = (letter or "").strip()
    changed = True
    while body and changed:
        changed = False
        stripped = _AI_HOOK.sub("", body, count=1)
        if stripped != body:
            body = stripped.lstrip()
            changed = True
    return body


def ensure_opening_hook(letter: str, hook: str | None = None, enforce: bool = True) -> str:
    hook_text = (OPENING_HOOK if hook is None else hook).strip()
    body = strip_ai_hook(letter)
    if not enforce or not hook_text:
        return body
    if not body:
        return hook_text
    suffixes = [part.strip() for part in re.split(r"(?<=\.)\s+", hook_text) if part.strip()]
    changed = True
    while body and changed:
        changed = False
        if body.lower().startswith(hook_text.lower()):
            body = body[len(hook_text) :].lstrip()
            changed = True
            continue
        stripped = _AI_HOOK.sub("", body, count=1)
        if stripped != body:
            body = stripped.lstrip()
            changed = True
            continue
        for suffix in reversed(suffixes):
            if len(suffix) > 12 and body.lower().startswith(suffix.lower()):
                body = body[len(suffix) :].lstrip()
                changed = True
                break
    if not body:
        return hook_text
    return f"{hook_text}\n\n{body}"


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


def dump_critique(result: CritiqueResult | None) -> str:
    if result is None:
        return "{}"
    return json.dumps(result.model_dump(), ensure_ascii=False)


def load_critique(proposal: Proposal | None) -> CritiqueResult | None:
    if proposal is None:
        return None
    raw = getattr(proposal, "critique_json", "") or ""
    if not raw.strip() or raw.strip() == "{}":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return CritiqueResult.model_validate(parsed)
    except Exception:
        return None


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
