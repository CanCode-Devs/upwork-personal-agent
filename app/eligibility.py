from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel

from app.models import RuntimeSettings

_US = r"(?:u\.?\s*s\.?a?\.?|united\s+states)"
_W2 = r"w[\s-]?2"
_C2C = r"c\s*2\s*c|corp(?:oration)?[\s-]*to[\s-]*corp(?:oration)?"
_ONSITE = r"on[\s-]?site|onsite|in[\s-]?office"


class EligibilityKind(StrEnum):
    work_auth = "work_auth"
    w2 = "w2"
    onsite = "onsite"


class EligibilityHit(BaseModel):
    kind: EligibilityKind
    reason: str


_WORK_AUTH = (
    re.compile(rf"\b{_US}\s+citizens?\b", re.I),
    re.compile(r"\bgreen\s+cards?\b", re.I),
    re.compile(r"\b(?:green\s+card|permanent\s+resident)\s+holders?\b", re.I),
    re.compile(
        r"\b(?:no|not|cannot|can't|unable\s+to)\s+(?:offer\s+)?(?:visa\s+)?sponsorship\b"
        r"|\bnot\s+sponsoring\b"
        r"|\bcannot\s+sponsor\b",
        re.I,
    ),
    re.compile(rf"\bauthorized\s+to\s+work\s+in\s+(?:the\s+)?{_US}\b", re.I),
    re.compile(rf"\bno\s+(?:{_C2C})\s+or\s+sponsorship\b", re.I),
)

_W2_ONLY = (
    re.compile(rf"\b{_W2}\s+(?:only|contract(?:\s+only)?|employees?\s+only)\b", re.I),
    re.compile(rf"\b(?:no|not)\s+(?:{_C2C})\b", re.I),
)

_ONSITE = (
    re.compile(rf"\b(?:{_ONSITE})\s+(?:only|required|mandatory)\b", re.I),
    re.compile(r"\bmust\s+be\s+(?:located|based|living|residing)\s+in\b", re.I),
    re.compile(r"\blocal\s+candidates?\s+only\b|\bcandidates?\s+must\s+be\s+local\b", re.I),
    re.compile(r"\brelocation\s+required\b|\bmust\s+relocate\b", re.I),
    re.compile(rf"\bhybrid\s+(?:\d+\s+)?(?:days?|{_ONSITE})\b", re.I),
    re.compile(
        rf"\b(?:candidates?|applicants?|talent|freelancers?|developers?|engineers?)\s+"
        rf"(?:must\s+be|should\s+be|need\s+to\s+be)\s+{_US}[\s-]*based\b",
        re.I,
    ),
    re.compile(rf"\b{_US}[\s-]*based\s+(?:candidates?|applicants?|talent)\s+only\b", re.I),
)

_REASONS: dict[EligibilityKind, str] = {
    EligibilityKind.work_auth: "US work authorization",
    EligibilityKind.w2: "W-2 / no C2C",
    EligibilityKind.onsite: "on-site / local-only",
}


def _any_match(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _contractor_w2(value: str) -> bool:
    lowered = value.lower().strip()
    if not lowered:
        return False
    if "1099" in lowered or "independent" in lowered or "freelancer" in lowered:
        return False
    return bool(re.search(rf"\b{_W2}\b", lowered))


def eligibility_hits(text: str, contractor_type: str = "") -> list[EligibilityHit]:
    blob = text or ""
    found: list[EligibilityHit] = []
    seen: set[EligibilityKind] = set()

    def add(kind: EligibilityKind) -> None:
        if kind in seen:
            return
        seen.add(kind)
        found.append(EligibilityHit(kind=kind, reason=_REASONS[kind]))

    if _any_match(_WORK_AUTH, blob):
        add(EligibilityKind.work_auth)
    if _any_match(_W2_ONLY, blob) or _contractor_w2(contractor_type):
        add(EligibilityKind.w2)
    if _any_match(_ONSITE, blob):
        add(EligibilityKind.onsite)
    return found


def hard_block_reasons(
    text: str,
    runtime: RuntimeSettings,
    contractor_type: str = "",
) -> list[str]:
    enabled = {
        EligibilityKind.work_auth: runtime.skip_us_work_auth,
        EligibilityKind.w2: runtime.skip_w2_only,
        EligibilityKind.onsite: runtime.skip_onsite,
    }
    reasons: list[str] = []
    for hit in eligibility_hits(text, contractor_type):
        if enabled.get(hit.kind):
            reasons.append(f"hard_block: {hit.reason}")
    return reasons
