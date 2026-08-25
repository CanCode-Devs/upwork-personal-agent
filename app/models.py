from datetime import datetime
from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"
    submitted = "submitted"
    submit_failed = "submit_failed"
    expired = "expired"
    skipped = "skipped"


class InboxSort(StrEnum):
    recent = "recent"
    oldest = "oldest"
    score = "score"
    score_low = "score_low"


class FunnelStatus(StrEnum):
    new = "new"
    skipped = "skipped"
    pitch_drafted = "pitch_drafted"
    pending_review = "pending_review"
    submitted = "submitted"


class AutonomyMode(StrEnum):
    manual = "manual"
    auto_above_threshold = "auto_above_threshold"
    fully_auto = "fully_auto"


class EnforcementLevel(StrEnum):
    strict_block = "strict_block"
    soft_penalty = "soft_penalty"


class PreferenceCategory(StrEnum):
    budget = "budget"
    tech_stack = "tech_stack"
    client_metrics = "client_metrics"
    proposal_style = "proposal_style"


class FeedbackOutcome(StrEnum):
    hired = "hired"
    shortlisted = "shortlisted"
    viewed = "viewed"
    ignored = "ignored"
    rejected = "rejected"
    approved = "approved"
    edited = "edited"
    expired = "expired"


class PitchTone(StrEnum):
    assertive = "assertive"
    technical_peer = "technical_peer"
    consultative = "consultative"


class EmbeddingSource(StrEnum):
    portfolio = "portfolio"
    job = "job"
    proposal = "proposal"


class WorkOrigin(StrEnum):
    upwork = "upwork"
    agent = "agent"


class WorkKind(StrEnum):
    project = "project"
    job_history = "job_history"
    employment = "employment"
    proposal = "proposal"


class FreelancerProfile(BaseModel):
    name: str = ""
    title: str = ""
    skills: list[str] = Field(default_factory=list)
    hourly_rate: int | None = None
    voice: str = "concise, specific, no fluff"
    working_hours: str = ""
    exclude_keywords: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    upwork_overview: str = ""


class ScoreResult(BaseModel):
    score: int = Field(ge=0, le=100)
    reason: str
    should_apply: bool
    go: bool = True
    breakdown: list[str] = Field(default_factory=list)


class ProposalMilestone(BaseModel):
    description: str
    amount: float
    title: str = ""


class ScreeningAnswer(BaseModel):
    question: str
    answer: str = ""


class ApplyHighlight(BaseModel):
    kind: str
    id: str
    title: str = ""
    selected: bool = False
    detail: str = ""


class DraftResult(BaseModel):
    cover_letter: str
    matched_context: list[str] = Field(default_factory=list)
    milestones: list[ProposalMilestone] = Field(default_factory=list)
    screening_answers: list[ScreeningAnswer] = Field(default_factory=list)
    portfolio_project_ids: list[str] = Field(default_factory=list)
    certificate_ids: list[str] = Field(default_factory=list)
    job_history_ids: list[str] = Field(default_factory=list)
    profile_history_ids: list[str] = Field(default_factory=list)


class LearnPreferenceArgs(BaseModel):
    category: PreferenceCategory
    rule: str
    enforcement_level: EnforcementLevel = EnforcementLevel.soft_penalty


class UpdatePortfolioArgs(BaseModel):
    project_title: str
    tech_stack: list[str] = Field(default_factory=list)
    outcomes_achieved: str = ""
    associated_keywords: list[str] = Field(default_factory=list)
    description: str = ""
    kind: WorkKind = WorkKind.project


class LogFeedbackArgs(BaseModel):
    job_id: str
    outcome: FeedbackOutcome
    client_notes: str = ""


class FetchLiveJobsArgs(BaseModel):
    query_keywords: str
    category_id: str = ""
    limit: int = 30


class RetrieveContextArgs(BaseModel):
    job_description_text: str
    top_k_results: int = 6


class ScoringMatrixArgs(BaseModel):
    client_rating: float | None = None
    client_payment_status: str = ""
    job_budget: float | None = None
    estimated_duration: str = ""
    job_text: str = ""
    title: str = ""
    timezone: str = ""
    job_type: str = ""
    client_hires: int | None = None
    proposal_count: int | None = None
    hire_rate: float | None = None
    invites_sent: int | None = None
    interviewing: int | None = None
    attachment_text: str = ""
    price_label: str = ""


class GeneratePitchArgs(BaseModel):
    job_id: str
    tone: PitchTone = PitchTone.consultative
    focus_points: list[str] = Field(default_factory=list)


class TrackFunnelArgs(BaseModel):
    job_id: str
    status: FunnelStatus


class SubmitProposalArgs(BaseModel):
    job_id: str
    cover_letter: str
    boost_connects: int = 0
    charged_amount: float | None = None
    milestones: list[ProposalMilestone] = Field(default_factory=list)
    screening_answers: list[ScreeningAnswer] = Field(default_factory=list)
    portfolio_project_ids: list[str] = Field(default_factory=list)
    certificate_ids: list[str] = Field(default_factory=list)
    attachment_uids: list[str] = Field(default_factory=list)


class ConnectsPanel(BaseModel):
    available: int | None = None
    apply_cost: int | None = None
    can_apply: bool | None = None
    boost_available: bool = False
    boost_reason: str = ""
    recommended_connects: int | None = None
    top_bids: list[int] = Field(default_factory=list)
    bid_count: int = 0
    bids_unknown: bool = False
    rationale: str = ""
    error: str = ""
    charged_amount: float | None = None
    remaining_after_apply: int | None = None
    milestones_allowed: bool = False
    screening_questions: list[str] = Field(default_factory=list)


class PreferenceRuleOut(BaseModel):
    id: int
    category: str
    rule: str
    enforcement_level: str
    active: bool


class PortfolioItemOut(BaseModel):
    id: int
    project_title: str
    tech_stack: list[str]
    outcomes_achieved: str
    associated_keywords: list[str]
    origin: str = "agent"
    kind: str = "project"
    description: str = ""
    editable: bool = True


class ContextMatch(BaseModel):
    source_type: str
    source_id: int
    score: float
    text: str
    title: str = ""
    origin: str = "agent"


class RuntimeSettings(BaseModel):
    autonomy_mode: AutonomyMode = AutonomyMode.manual
    auto_submit_threshold: int = 85
    min_score: int = 70
    min_hourly: int | None = None
    min_fixed: int | None = None
    require_verified_payment: bool = False
    min_client_rating: float | None = None
    min_client_hires: int | None = None
    max_proposal_count: int | None = None
    prefer_timezones: str = ""


class JobPayload(TypedDict, total=False):
    id: str
    title: str
    description: str
    budget: str
    url: str
    client: str
    raw: str
    client_rating: float
    client_payment_status: str
    estimated_duration: str
    job_budget_value: float
    price_label: str
    timezone: str
    job_type: str
    client_details: str
    client_hires: int
    proposal_count: int
    hire_rate: float
    invites_sent: int
    interviewing: int


class HighlightPicks(TypedDict):
    portfolio_project_ids: list[str]
    certificate_ids: list[str]
    job_history_ids: list[str]
    profile_history_ids: list[str]


class ToolCallArgs(TypedDict, total=False):
    query: str
    keyword: str
    search: str
    q: str
    job_id: str
    jobId: str
    cover_letter: str
    coverLetter: str


class SessionUser(TypedDict):
    username: str


class InboxCounts(TypedDict):
    pending_review: int
    submitted: int
    rejected: int
    submit_failed: int
    expired: int
    applied: int


class McpStatus(TypedDict):
    connected: bool
    tools: list[str]
    error: str


class JobRow(TypedDict, total=False):
    id: int
    upwork_id: str
    title: str
    budget: str | None
    url: str | None
    score: int | None
    score_reason: str | None
    status: str
    created_at: datetime
    expires_at: datetime | None
    draft_text: str
    edited_text: str | None


class ToolSpec(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]
