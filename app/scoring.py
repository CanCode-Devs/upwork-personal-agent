from app.config import Settings
from app.llm import llm_draft, llm_score
from app.models import DraftResult, FreelancerProfile, JobPayload, ScoreResult


def _blob(job: JobPayload) -> str:
    parts = [
        job.get("title") or "",
        job.get("description") or "",
        job.get("budget") or "",
        job.get("client") or "",
    ]
    return " ".join(parts).lower()


def heuristic_score(job: JobPayload, profile: FreelancerProfile, min_score: int) -> ScoreResult:
    text = _blob(job)
    hits = [skill for skill in profile.skills if skill.lower() in text]
    excludes = [word for word in profile.exclude_keywords if word.lower() in text]
    score = 40
    if hits:
        score += min(40, 10 * len(hits))
    if job.get("budget"):
        score += 5
    score -= 25 * len(excludes)
    score = max(0, min(100, score))
    reason_bits: list[str] = []
    if hits:
        reason_bits.append("skills: " + ", ".join(hits))
    if excludes:
        reason_bits.append("excluded: " + ", ".join(excludes))
    if not reason_bits:
        reason_bits.append("limited keyword overlap with profile")
    return ScoreResult(
        score=score,
        reason="; ".join(reason_bits),
        should_apply=score >= min_score and not excludes,
        go=score >= min_score and not excludes,
        breakdown=reason_bits,
    )


def score_job(
    job: JobPayload,
    profile: FreelancerProfile,
    settings: Settings,
) -> ScoreResult:
    if settings.openai_api_key:
        try:
            return llm_score(job, profile, settings)
        except Exception:
            return heuristic_score(job, profile, settings.min_score)
    return heuristic_score(job, profile, settings.min_score)


def draft_proposal(
    job: JobPayload,
    profile: FreelancerProfile,
    settings: Settings,
) -> DraftResult:
    return llm_draft(job, profile, settings)
