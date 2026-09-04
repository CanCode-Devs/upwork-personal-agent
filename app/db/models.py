from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upwork_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    budget: Mapped[str | None] = mapped_column(String(256), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    client_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_score_breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    job_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    client_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_on_upwork: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    proposals: Mapped[list["Proposal"]] = relationship(back_populates="job")
    events: Mapped[list["Event"]] = relationship(back_populates="job")


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    draft_text: Mapped[str] = mapped_column(Text, default="")
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    submit_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    milestones_json: Mapped[str] = mapped_column(Text, default="[]")
    screening_json: Mapped[str] = mapped_column(Text, default="[]")
    apply_json: Mapped[str] = mapped_column(Text, default="{}")
    critique_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    job: Mapped[Job] = relationship(back_populates="proposals")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job | None] = relationship(back_populates="events")
    user: Mapped["User | None"] = relationship()


class PreferenceRule(Base):
    __tablename__ = "preferences_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    rule: Mapped[str] = mapped_column(Text)
    enforcement_level: Mapped[str] = mapped_column(String(32), default="soft_penalty")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortfolioItem(Base):
    __tablename__ = "portfolio_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_title: Mapped[str] = mapped_column(String(256))
    tech_stack: Mapped[str] = mapped_column(Text, default="[]")
    outcomes_achieved: Mapped[str] = mapped_column(Text, default="")
    associated_keywords: Mapped[str] = mapped_column(Text, default="[]")
    origin: Mapped[str] = mapped_column(String(32), default="agent", index=True)
    kind: Mapped[str] = mapped_column(String(32), default="project", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True, unique=True, index=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UpworkProfile(Base):
    __tablename__ = "upwork_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    overview: Mapped[str] = mapped_column(Text, default="")
    skills_json: Mapped[str] = mapped_column(Text, default="[]")
    hourly_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UpworkApplication(Base):
    __tablename__ = "upwork_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    posting_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    proposal_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(64), default="")
    rate: Mapped[str] = mapped_column(String(64), default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeedbackLog(Base):
    __tablename__ = "feedback_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    upwork_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    client_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmbeddingIndex(Base):
    __tablename__ = "embedding_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    vector_offset: Mapped[int] = mapped_column(Integer)
    text_preview: Mapped[str] = mapped_column(Text, default="")


class AppRuntimeSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    autonomy_mode: Mapped[str] = mapped_column(String(32), default="manual")
    auto_submit_threshold: Mapped[int] = mapped_column(Integer, default=85)
    min_score: Mapped[int] = mapped_column(Integer, default=70)
    min_client_score: Mapped[int] = mapped_column(Integer, default=50)
    min_hourly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_fixed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    require_verified_payment: Mapped[bool] = mapped_column(Boolean, default=False)
    min_client_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_client_hires: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_proposal_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prefer_timezones: Mapped[str] = mapped_column(Text, default="")
    skip_us_work_auth: Mapped[bool] = mapped_column(Boolean, default=True)
    skip_w2_only: Mapped[bool] = mapped_column(Boolean, default=True)
    skip_onsite: Mapped[bool] = mapped_column(Boolean, default=True)
    skip_entry_level: Mapped[bool] = mapped_column(Boolean, default=True)
    job_type_filter: Mapped[str] = mapped_column(String(16), default="any")
    engagement_filter: Mapped[str] = mapped_column(String(16), default="any")
    blocked_client_countries: Mapped[str] = mapped_column(Text, default="")
    min_client_spend: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_connects_cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_search_queries: Mapped[str] = mapped_column(Text, default="[]")
    dismissed_search_queries: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProposalSettings(Base):
    __tablename__ = "proposal_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opening_hook: Mapped[str] = mapped_column(Text, default="")
    enforce_opening_hook: Mapped[bool] = mapped_column(Boolean, default=True)
    tone: Mapped[str] = mapped_column(String(32), default="consultative")
    letter_structure: Mapped[str] = mapped_column(Text, default="")
    role_letter_structure: Mapped[str] = mapped_column(Text, default="")
    must_include: Mapped[str] = mapped_column(Text, default="")
    never_say: Mapped[str] = mapped_column(Text, default="")
    extra_instructions: Mapped[str] = mapped_column(Text, default="")
    target_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    milestone_instructions: Mapped[str] = mapped_column(Text, default="")
    milestone_stages: Mapped[str] = mapped_column(Text, default="[]")
    milestone_min: Mapped[int] = mapped_column(Integer, default=3)
    milestone_max: Mapped[int] = mapped_column(Integer, default=5)
    screening_instructions: Mapped[str] = mapped_column(Text, default="")
    apply_questions_instructions: Mapped[str] = mapped_column(Text, default="")
    example_count: Mapped[int] = mapped_column(Integer, default=2)
    critique_rounds: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProposalExample(Base):
    __tablename__ = "proposal_examples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    job_post: Mapped[str] = mapped_column(Text, default="")
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MessageRoom(Base):
    __tablename__ = "message_rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    context_type: Mapped[str] = mapped_column(String(64), default="")
    context_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    counterpart: Mapped[str] = mapped_column(String(256), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    unread: Mapped[int] = mapped_column(Integer, default=0)
    send_status: Mapped[str] = mapped_column(String(32), default="idle")
    send_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="room")
    draft: Mapped["MessageDraft | None"] = relationship(back_populates="room", uselist=False)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_pk: Mapped[int] = mapped_column(ForeignKey("message_rooms.id"), index=True)
    upwork_message_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    sender: Mapped[str] = mapped_column(String(32), default="client")
    body: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    room: Mapped[MessageRoom] = relationship(back_populates="messages")


class MessageDraft(Base):
    __tablename__ = "message_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_pk: Mapped[int] = mapped_column(ForeignKey("message_rooms.id"), unique=True, index=True)
    suggested_text: Mapped[str] = mapped_column(Text, default="")
    suggested_intents: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    room: Mapped[MessageRoom] = relationship(back_populates="draft")


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
