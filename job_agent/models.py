from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    NEW = "new"
    ANALYZED = "analyzed"
    REVIEWED = "reviewed"
    FAVORITE = "favorite"
    IGNORED = "ignored"
    DRAFT_CREATED = "draft_created"
    READY_TO_APPLY = "ready_to_apply"
    NEEDS_CLARIFICATION = "needs_clarification"
    APPLIED = "applied"
    CLOSED = "closed"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"
    ERROR = "error"


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    NEEDS_CLARIFICATION = "needs_clarification"
    APPROVED = "approved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    VIEWED = "viewed"
    CLIENT_REPLIED = "client_replied"
    INTERVIEW = "interview"
    TEST_TASK = "test_task"
    NEGOTIATION = "negotiation"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"
    ERROR = "error"


class SearchFilters(BaseModel):
    queries: list[str] = Field(default_factory=list)
    remote_only: bool = True
    max_age_hours: int = 72
    # The first page of a generic query is often dominated by adjacent roles.
    # A broader candidate pool lets the deterministic/LLM analysis decide;
    # it must not silently hide suitable FastAPI/RAG listings after eight rows.
    max_results: int = 40


class AuthStatus(BaseModel):
    authenticated: bool
    detail: str = ""


class RawJob(BaseModel):
    external_job_id: str
    site: str
    url: str
    title: str
    company: str | None = None
    budget: str | None = None
    work_format: str | None = None
    published_at: datetime | None = None
    summary: str = ""


class RawJobDetails(RawJob):
    description: str = ""
    requirements: list[str] = Field(default_factory=list)
    desired_skills: list[str] = Field(default_factory=list)
    response_count: int | None = None
    deadline: datetime | None = None
    normalized_text: str = ""


class NormalizedVacancy(BaseModel):
    """Uniform, evidence-only representation produced from a raw listing."""

    title: str = ""
    summary: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    stack: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    budget: str | None = None
    work_format: str | None = None
    response_questions: list[str] = Field(default_factory=list)

    def as_text(self) -> str:
        sections = [("ЗАДАЧА", [self.summary]), ("ЧТО НУЖНО СДЕЛАТЬ", self.responsibilities), ("ТРЕБОВАНИЯ", self.requirements), ("СТЕК", self.stack), ("ОГРАНИЧЕНИЯ", self.constraints), ("ЧТО УКАЗАТЬ В ОТКЛИКЕ", self.response_questions)]
        lines = [f"ВАКАНСИЯ: {self.title}" if self.title else "ВАКАНСИЯ"]
        if self.budget:
            lines.append(f"БЮДЖЕТ: {self.budget}")
        if self.work_format:
            lines.append(f"ФОРМАТ: {self.work_format}")
        for heading, entries in sections:
            values = [item.strip() for item in entries if item and item.strip()]
            if values:
                lines.extend(["", heading, *(f"• {item}" for item in values)])
        return "\n".join(lines)


class AnalysisResult(BaseModel):
    summary: str
    job_type: str = "employment"
    match_score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    recommendation: str
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    critical_requirements: list[str] = Field(default_factory=list)
    possible_risks: list[str] = Field(default_factory=list)
    estimated_complexity: str = "unknown"
    estimated_effort: str = "unknown"
    budget_assessment: str = "unknown"
    reasoning: str
    response_strategy: list[str] = Field(default_factory=list)
    questions_for_client: list[str] = Field(default_factory=list)
    candidate_level_fit: str = "unknown"
    required_skills_fit: dict[str, list[str]] = Field(default_factory=dict)
    best_portfolio_project: str | None = None
    safe_claims_for_application: list[str] = Field(default_factory=list)
    claims_to_avoid: list[str] = Field(default_factory=list)
    auto_apply_allowed: bool = False


class PreparedApplication(BaseModel):
    job_id: int
    site: str
    body: str
    external_job_id: str | None = None
    title: str | None = None
    price: str | None = None
    duration: str | None = None
    requires_confirmation: bool = True
    form_answers: dict[str, str] = Field(default_factory=dict)


class ClarificationInput(BaseModel):
    """A required form field that cannot be safely inferred from the profile."""

    question: str
    kind: str = "other"
    field_name: str = ""
    source: str = "application_form"
    required: bool = True
    artifact_path: str | None = None


class SubmissionResult(BaseModel):
    success: bool
    confirmed: bool
    external_application_id: str | None = None
    detail: str = ""
    screenshot_path: str | None = None
    clarifications: list[ClarificationInput] = Field(default_factory=list)


class RawMessage(BaseModel):
    external_message_id: str
    site: str
    conversation_id: str
    sender: str = ""
    body: str
    received_at: datetime | None = None
    job_external_id: str | None = None
    is_unread: bool = True
    category: str = "unknown"


class ExternalApplicationStatus(BaseModel):
    external_application_id: str
    status: str
    detail: str = ""


class SendMessageResult(BaseModel):
    success: bool
    confirmed: bool
    detail: str = ""
    screenshot_path: str | None = None
