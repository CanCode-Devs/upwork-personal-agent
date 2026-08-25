from __future__ import annotations

import re
from typing import Literal

EngagementKind = Literal["role", "project"]

_ROLE_PATTERNS = [
    re.compile(r"\bwe are looking for\b", re.I),
    re.compile(r"(?m)^role:\s", re.I),
    re.compile(r"\blong[- ]term\b", re.I),
    re.compile(r"\bhours?\s*/\s*week\b", re.I),
    re.compile(r"\b\d+\s*(?:-|–|to)\s*\d+\s*h(?:ours?)?\b", re.I),
    re.compile(r"\b(?:min(?:imum)?|up to)\s+\d+\s*hours?\b", re.I),
    re.compile(r"\bpaid trial\b", re.I),
    re.compile(r"\btrial period\b", re.I),
    re.compile(r"\bwhat we look for\b", re.I),
    re.compile(r"\bresponsibilities\b", re.I),
    re.compile(r"\bjoin (?:the|our) team\b", re.I),
    re.compile(r"\bfull[- ]time\b", re.I),
    re.compile(r"\bpart[- ]time\b", re.I),
    re.compile(r"\bongoing\b", re.I),
    re.compile(r"\badding one (?:engineer|developer|person)\b", re.I),
    re.compile(r"\bnot a (?:series of )?one-off\b", re.I),
    re.compile(r"\bthis is not a role for\b", re.I),
]

_PROJECT_PATTERNS = [
    re.compile(r"\bneed someone to (?:build|create|develop|implement)\b", re.I),
    re.compile(r"\bby (?:monday|tuesday|wednesday|thursday|friday|end of (?:week|month))\b", re.I),
    re.compile(r"\bfixed[- ]price\b", re.I),
    re.compile(r"\bmilestone(?:s)?\b", re.I),
]


def classify_engagement(title: str, description: str, job_type: str = "") -> EngagementKind:
    blob = f"{title or ''}\n{description or ''}"
    role_hits = sum(1 for pattern in _ROLE_PATTERNS if pattern.search(blob))
    project_hits = sum(1 for pattern in _PROJECT_PATTERNS if pattern.search(blob))
    hourly = (job_type or "").lower().startswith("hourly")
    if hourly and role_hits >= 2:
        return "role"
    if role_hits >= 3 and role_hits > project_hits:
        return "role"
    return "project"
