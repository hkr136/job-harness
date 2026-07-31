from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("site", "external_job_id", name="uq_job_site_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site: Mapped[str] = mapped_column(String(64), index=True)
    external_job_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    company: Mapped[str | None] = mapped_column(String(512), nullable=True)
    budget: Mapped[str | None] = mapped_column(String(128), nullable=True)
    work_format: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnalysisRecord(Base):
    __tablename__ = "analyses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    payload_json: Mapped[str] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer, index=True)
    model: Mapped[str] = mapped_column(String(128), default="local")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApplicationRecord(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    site: Mapped[str] = mapped_column(String(64), index=True)
    draft: Mapped[str] = mapped_column(Text)
    final_text: Mapped[str] = mapped_column(Text, default="")
    offer_title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    offer_price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    offer_duration: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    external_application_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApplicationStatusHistoryRecord(Base):
    __tablename__ = "application_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(64), default="local")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClarificationRequestRecord(Base):
    __tablename__ = "clarification_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True, index=True)
    site: Mapped[str] = mapped_column(String(64), index=True)
    question: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(64), default="other", index=True)
    field_name: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(128), default="application_form")
    required: Mapped[bool] = mapped_column(default=True)
    state: Mapped[str] = mapped_column(String(32), default="open", index=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageRecord(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("site", "external_message_id", name="uq_message_site_external"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site: Mapped[str] = mapped_column(String(64), index=True)
    external_message_id: Mapped[str] = mapped_column(String(255))
    conversation_id: Mapped[str] = mapped_column(String(255), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    sender: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), default="unknown")
    is_unread: Mapped[bool] = mapped_column(default=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MessageReplyRecord(Base):
    """One locally prepared/sent reply per incoming message.

    The record is deliberately separate from the remote chat.  A draft is not
    evidence of a sent message; only an adapter-confirmed result can set the
    status to ``sent``.
    """

    __tablename__ = "message_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), unique=True, index=True)
    site: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(255), index=True)
    draft: Mapped[str] = mapped_column(Text, default="")
    final_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    confirmation_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentSessionRecord(Base):
    """One durable orchestration turn for a job, message, or background task."""

    __tablename__ = "agent_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(32), index=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(16), default="safe")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentEventRecord(Base):
    """Append-only tool transcript, mirroring the OMP execution event model."""

    __tablename__ = "agent_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(32), index=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    tool_name: Mapped[str] = mapped_column(String(64), default="")
    access_level: Mapped[str] = mapped_column(String(16), default="read")
    intent: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LLMUsageRecord(Base):
    """Durable, provider-reported usage telemetry.

    Token values are nullable rather than guessed: the authenticated Codex CLI
    does not currently report them to this process.
    """

    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(96), index=True)
    provider: Mapped[str] = mapped_column(String(96), default="unknown", index=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[str] = mapped_column(String(32), default="completed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ApplicationReviewRecord(Base):
    """One quality/truthfulness review for an application draft version."""

    __tablename__ = "application_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    score: Mapped[int] = mapped_column(Integer, index=True)
    approved: Mapped[bool] = mapped_column(default=False, index=True)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    rewrite_notes: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(96), default="unknown")
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunRecord(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    site: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueueTaskRecord(Base):
    __tablename__ = "queue_tasks"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_queue_task_idempotency"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    site: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="")
