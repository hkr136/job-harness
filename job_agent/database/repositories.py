from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from job_agent.database.models import (
    AgentEventRecord,
    AgentSessionRecord,
    AnalysisRecord,
    ApplicationRecord,
    ApplicationReviewRecord,
    ApplicationStatusHistoryRecord,
    Base,
    ClarificationRequestRecord,
    JobRecord,
    LLMUsageRecord,
    MessageRecord,
    MessageReplyRecord,
    QueueTaskRecord,
    RunRecord,
)
from job_agent.models import AnalysisResult, ClarificationInput, RawJobDetails, RawMessage


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalise SQLite's timezone-less datetime values before Python compares.

    SQLite does not preserve ``tzinfo`` even for timezone-aware SQLAlchemy
    columns.  Older records therefore come back naive while ``utcnow()`` is
    aware, which must never block an application policy check.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Store:
    _ACTIVE_APPLICATION_STATUSES = {
        "submitted",
        "viewed",
        "client_replied",
        "interview",
        "test_task",
        "negotiation",
        "accepted",
        "rejected",
    }

    def __init__(self, url: str) -> None:
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url)
        Base.metadata.create_all(self.engine)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Apply additive SQLite migrations without touching existing user records."""
        # SQLAlchemy creates the new table for new and legacy databases alike.
        # The explicit check makes the migration contract visible and idempotent.
        existing_tables = inspect(self.engine).get_table_names()
        if "clarification_requests" not in existing_tables:
            ClarificationRequestRecord.__table__.create(self.engine, checkfirst=True)
        if "message_replies" not in existing_tables:
            MessageReplyRecord.__table__.create(self.engine, checkfirst=True)
        if "agent_sessions" not in existing_tables:
            AgentSessionRecord.__table__.create(self.engine, checkfirst=True)
        if "agent_events" not in existing_tables:
            AgentEventRecord.__table__.create(self.engine, checkfirst=True)
        if "llm_usage" not in existing_tables:
            LLMUsageRecord.__table__.create(self.engine, checkfirst=True)
        if "application_reviews" not in existing_tables:
            ApplicationReviewRecord.__table__.create(self.engine, checkfirst=True)
        # Additive fields for marketplace offers. SQLite has no ALTER support
        # through SQLAlchemy metadata, so inspect before each idempotent column.
        columns = {column["name"] for column in inspect(self.engine).get_columns("applications")}
        with self.engine.begin() as connection:
            for name, declaration in (("offer_title", "VARCHAR(100)"), ("offer_price", "VARCHAR(64)"), ("offer_duration", "VARCHAR(32)")):
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE applications ADD COLUMN {name} {declaration}"))
        # Older databases may contain confirmed applications created before the
        # vacancy/application status synchronization existed. Repair only the
        # derived job state; application history and user data stay untouched.
        with Session(self.engine) as session, session.begin():
            confirmed = select(ApplicationRecord).where(
                ApplicationRecord.status.in_(self._ACTIVE_APPLICATION_STATUSES)
            )
            for application in session.scalars(confirmed):
                job = session.get(JobRecord, application.job_id)
                if job and job.status not in {"closed", "expired", "ignored"}:
                    job.status, job.checked_at = "applied", utcnow()
            self._reconcile_unread_message_duplicates(session)

    @staticmethod
    def _reconcile_unread_message_duplicates(session: Session) -> None:
        """Leave one active inbox item per unread site conversation.

        UI badges identify a conversation, not a stable remote message ID.  If a
        platform redraws the preview, preserve any reply decision on the oldest
        matching item and mark surplus local previews as already handled.
        """
        records = list(session.scalars(
            select(MessageRecord)
            .where(MessageRecord.is_unread.is_(True))
            .order_by(MessageRecord.received_at)
        ))
        grouped: dict[tuple[str, str], list[MessageRecord]] = {}
        for record in records:
            grouped.setdefault((record.site, record.conversation_id), []).append(record)
        for items in grouped.values():
            if len(items) < 2:
                continue
            reply_ids = set(session.scalars(
                select(MessageReplyRecord.message_id).where(
                    MessageReplyRecord.message_id.in_([item.id for item in items])
                )
            ))
            canonical = next((item for item in items if item.id in reply_ids), items[0])
            for item in items:
                if item.id != canonical.id:
                    item.is_unread = False

    def upsert_job(self, raw: RawJobDetails) -> tuple[JobRecord, bool]:
        text = " ".join([raw.title, raw.company or "", raw.description or raw.summary]).lower()
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        now = utcnow()
        with Session(self.engine) as session, session.begin():
            item = session.scalar(select(JobRecord).where(JobRecord.site == raw.site, JobRecord.external_job_id == raw.external_job_id))
            created = item is None
            if item is None:
                item = JobRecord(site=raw.site, external_job_id=raw.external_job_id, url=raw.url, title=raw.title, company=raw.company, budget=raw.budget, work_format=raw.work_format, published_at=raw.published_at, description=raw.description, normalized_text=raw.normalized_text or text, fingerprint=fingerprint, discovered_at=now, checked_at=now)
                session.add(item)
                session.flush()
            else:
                item.title, item.company, item.budget, item.work_format = raw.title, raw.company, raw.budget, raw.work_format
                item.description, item.normalized_text, item.fingerprint, item.checked_at = raw.description, raw.normalized_text or text, fingerprint, now
            session.expunge(item)
            return item, created

    def job_needs_analysis(self, raw: RawJobDetails) -> bool:
        """Compare the incoming normalized payload before replacing the stored job."""
        text = " ".join([raw.title, raw.company or "", raw.description or raw.summary]).lower()
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        with Session(self.engine) as session:
            item = session.scalar(select(JobRecord).where(JobRecord.site == raw.site, JobRecord.external_job_id == raw.external_job_id))
            if item is None:
                return True
            analysis = session.scalar(select(AnalysisRecord).where(AnalysisRecord.job_id == item.id))
            return item.fingerprint != fingerprint or analysis is None

    def save_analysis(self, job_id: int, analysis: AnalysisResult, model: str = "local", tokens: int = 0, cost_usd: float = 0.0) -> None:
        with Session(self.engine) as session, session.begin():
            record = session.scalar(select(AnalysisRecord).where(AnalysisRecord.job_id == job_id))
            if record is None:
                record = AnalysisRecord(job_id=job_id, payload_json=analysis.model_dump_json(), score=analysis.match_score, model=model, tokens=tokens, cost_usd=cost_usd, created_at=utcnow())
                session.add(record)
            else:
                record.payload_json, record.score, record.model, record.tokens, record.cost_usd, record.created_at = analysis.model_dump_json(), analysis.match_score, model, tokens, cost_usd, utcnow()
            job = session.get(JobRecord, job_id)
            if job and job.status != "needs_clarification":
                job.status = "analyzed"

    def save_llm_usage(
        self, *, role: str, action: str, provider: str, model: str | None,
        subject_type: str | None = None, subject_id: int | None = None,
        input_tokens: int | None = None, output_tokens: int | None = None,
        total_tokens: int | None = None, cost_usd: float | None = None,
        result: str = "completed",
    ) -> LLMUsageRecord:
        with Session(self.engine) as session, session.begin():
            record = LLMUsageRecord(
                role=role, action=action, provider=provider, model=model,
                subject_type=subject_type, subject_id=subject_id,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens, cost_usd=cost_usd, result=result,
                created_at=utcnow(),
            )
            session.add(record)
            session.flush()
            session.expunge(record)
            return record

    def list_llm_usage(self, since: datetime | None = None) -> list[LLMUsageRecord]:
        with Session(self.engine) as session:
            stmt = select(LLMUsageRecord).order_by(LLMUsageRecord.created_at.desc())
            if since is not None:
                stmt = stmt.where(LLMUsageRecord.created_at >= as_utc(since))
            records = list(session.scalars(stmt))
            for record in records:
                session.expunge(record)
            return records

    def latest_llm_usage(self, role: str) -> LLMUsageRecord | None:
        with Session(self.engine) as session:
            record = session.scalar(
                select(LLMUsageRecord)
                .where(LLMUsageRecord.role == role)
                .order_by(LLMUsageRecord.created_at.desc())
                .limit(1)
            )
            if record is not None:
                session.expunge(record)
            return record

    def llm_usage_summary(self, since: datetime | None = None) -> list[dict[str, object]]:
        groups: dict[tuple[str, str, str], dict[str, object]] = {}
        for item in self.list_llm_usage(since):
            key = (item.provider, item.model or "default", item.action)
            group = groups.setdefault(key, {"provider": key[0], "model": key[1], "action": key[2], "calls": 0, "tokens": 0, "reported_calls": 0, "cost_usd": 0.0, "failed": 0})
            group["calls"] = int(group["calls"]) + 1
            if item.total_tokens is not None:
                group["tokens"] = int(group["tokens"]) + item.total_tokens
                group["reported_calls"] = int(group["reported_calls"]) + 1
            if item.cost_usd is not None:
                group["cost_usd"] = float(group["cost_usd"]) + item.cost_usd
            if item.result != "completed":
                group["failed"] = int(group["failed"]) + 1
        return sorted(groups.values(), key=lambda item: (str(item["provider"]), str(item["action"])))

    def save_application_review(
        self, application_id: int, job_id: int, *, score: int, approved: bool,
        reasons: list[str], rewrite_notes: str, provider: str, model: str | None,
    ) -> ApplicationReviewRecord:
        with Session(self.engine) as session, session.begin():
            version = int(session.scalar(select(ApplicationReviewRecord.version).where(
                ApplicationReviewRecord.application_id == application_id
            ).order_by(ApplicationReviewRecord.version.desc()).limit(1)) or 0) + 1
            record = ApplicationReviewRecord(
                application_id=application_id, job_id=job_id, version=version,
                score=max(0, min(100, score)), approved=approved,
                reasons_json=json.dumps(reasons, ensure_ascii=False), rewrite_notes=rewrite_notes,
                provider=provider, model=model, created_at=utcnow(),
            )
            session.add(record)
            session.flush()
            session.expunge(record)
            return record

    def latest_application_review(self, application_id: int) -> ApplicationReviewRecord | None:
        with Session(self.engine) as session:
            record = session.scalar(select(ApplicationReviewRecord).where(
                ApplicationReviewRecord.application_id == application_id
            ).order_by(ApplicationReviewRecord.version.desc()).limit(1))
            if record is not None:
                session.expunge(record)
            return record

    def list_jobs(self, min_score: int = 0, site: str | None = None, status: str | None = None) -> list[tuple[JobRecord, AnalysisRecord | None]]:
        with Session(self.engine) as session:
            stmt = select(JobRecord, AnalysisRecord).outerjoin(AnalysisRecord, AnalysisRecord.job_id == JobRecord.id)
            if site: stmt = stmt.where(JobRecord.site == site)
            if status: stmt = stmt.where(JobRecord.status == status)
            rows = session.execute(stmt).all()
            return sorted([row for row in rows if row[1] is None or row[1].score >= min_score], key=lambda r: ((r[1].score if r[1] else 0), r[0].discovered_at), reverse=True)

    def get_job(self, job_id: int) -> tuple[JobRecord, AnalysisResult | None]:
        with Session(self.engine) as session:
            job = session.get(JobRecord, job_id)
            if job is None: raise ValueError(f"Unknown job ID: {job_id}")
            result = session.scalar(select(AnalysisRecord).where(AnalysisRecord.job_id == job_id))
            session.expunge(job)
            return job, AnalysisResult.model_validate_json(result.payload_json) if result else None

    def get_analysis_record(self, job_id: int) -> AnalysisRecord | None:
        with Session(self.engine) as session:
            record = session.scalar(select(AnalysisRecord).where(AnalysisRecord.job_id == job_id))
            if record is not None:
                session.expunge(record)
            return record

    def get_job_context(self, job_id: int) -> dict[str, object]:
        """One complete local work-item receipt for UI and agent tools.

        Dynamic job-search state deliberately lives in SQLite. Callers should
        not stitch a vacancy, its draft and its clarification state from
        separate ad-hoc lookups, which is how an existing draft became hidden
        from the chat agent in the first place.
        """
        job, analysis = self.get_job(job_id)
        try:
            application = self.get_application_for_job(job_id)
            application_data: dict[str, object] | None = {
                "id": application.id,
                "status": application.status,
                "draft": application.final_text or application.draft,
                "offer": {
                    "title": application.offer_title,
                    "price": application.offer_price,
                    "duration": application.offer_duration,
                },
                "submitted_at": application.submitted_at.isoformat() if application.submitted_at else None,
            }
        except ValueError:
            application_data = None
        with Session(self.engine) as session:
            clarifications = list(session.scalars(select(ClarificationRequestRecord).where(ClarificationRequestRecord.job_id == job_id)))
        return {
            "vacancy": {
                "id": job.id,
                "site": job.site,
                "title": job.title,
                "company": job.company,
                "url": job.url,
                "status": job.status,
                "budget": job.budget,
                "description": job.normalized_text.removeprefix("[normalized]\n") or job.description,
            },
            "analysis": analysis.model_dump(mode="json") if analysis else None,
            "application": application_data,
            "clarifications": [
                {"id": item.id, "kind": item.kind, "question": item.question, "state": item.state, "answer": item.answer}
                for item in clarifications
            ],
        }

    def set_job_status(self, job_id: int, status: str) -> None:
        with Session(self.engine) as session, session.begin():
            job = session.get(JobRecord, job_id)
            if job is None:
                raise ValueError(f"Unknown job ID: {job_id}")
            job.status, job.checked_at = status, utcnow()

    def create_clarifications(
        self,
        job_id: int,
        site: str,
        items: list[ClarificationInput],
        application_id: int | None = None,
    ) -> list[ClarificationRequestRecord]:
        """Persist unknown required fields and block the job before any submission."""
        if not items:
            return []
        with Session(self.engine) as session, session.begin():
            job = session.get(JobRecord, job_id)
            if job is None:
                raise ValueError(f"Unknown job ID: {job_id}")
            created: list[ClarificationRequestRecord] = []
            for item in items:
                existing = session.scalar(select(ClarificationRequestRecord).where(
                    ClarificationRequestRecord.job_id == job_id,
                    ClarificationRequestRecord.question == item.question,
                    ClarificationRequestRecord.field_name == item.field_name,
                    ClarificationRequestRecord.state.in_(("open", "answered")),
                ))
                if existing is not None:
                    continue
                record = ClarificationRequestRecord(
                    job_id=job_id,
                    application_id=application_id,
                    site=site,
                    question=item.question,
                    kind=item.kind,
                    field_name=item.field_name,
                    source=item.source,
                    required=item.required,
                    artifact_path=item.artifact_path,
                    created_at=utcnow(),
                )
                session.add(record)
                session.flush()
                session.expunge(record)
                created.append(record)
            if created:
                job.status, job.checked_at = "needs_clarification", utcnow()
                if application_id:
                    application = session.get(ApplicationRecord, application_id)
                    if application and application.status not in {"submitted", "viewed", "client_replied", "interview", "accepted", "rejected"}:
                        application.status = "needs_clarification"
            return created

    def list_clarifications(self, job_id: int | None = None, include_resolved: bool = False) -> list[ClarificationRequestRecord]:
        with Session(self.engine) as session:
            stmt = select(ClarificationRequestRecord).order_by(ClarificationRequestRecord.created_at.desc())
            if job_id is not None:
                stmt = stmt.where(ClarificationRequestRecord.job_id == job_id)
            if not include_resolved:
                stmt = stmt.where(ClarificationRequestRecord.state != "resolved")
            records = list(session.scalars(stmt))
            for record in records:
                session.expunge(record)
            return records

    def get_clarification(self, request_id: int) -> ClarificationRequestRecord:
        with Session(self.engine) as session:
            record = session.get(ClarificationRequestRecord, request_id)
            if record is None:
                raise ValueError(f"Unknown clarification request #{request_id}")
            session.expunge(record)
            return record

    def answer_clarification(self, request_id: int, answer: str, scope: str) -> ClarificationRequestRecord:
        if scope not in {"profile", "vacancy"}:
            raise ValueError("Clarification scope must be profile or vacancy")
        if not answer.strip():
            raise ValueError("Clarification answer cannot be empty")
        with Session(self.engine) as session, session.begin():
            record = session.get(ClarificationRequestRecord, request_id)
            if record is None:
                raise ValueError(f"Unknown clarification request #{request_id}")
            if record.state == "resolved":
                raise ValueError("Resolved clarification requests cannot be changed")
            record.answer, record.answer_scope, record.state, record.answered_at = answer.strip(), scope, "answered", utcnow()
            session.flush()
            session.expunge(record)
            return record

    def resolve_clarifications(self, job_id: int) -> tuple[bool, list[ClarificationRequestRecord]]:
        with Session(self.engine) as session, session.begin():
            records = list(session.scalars(select(ClarificationRequestRecord).where(ClarificationRequestRecord.job_id == job_id)))
            outstanding = [record for record in records if record.required and record.state == "open"]
            if outstanding:
                for record in records:
                    session.expunge(record)
                return False, outstanding
            for record in records:
                if record.state == "answered":
                    record.state, record.resolved_at = "resolved", utcnow()
            job = session.get(JobRecord, job_id)
            if job is None:
                raise ValueError(f"Unknown job ID: {job_id}")
            job.status, job.checked_at = "ready_to_apply", utcnow()
            application = session.scalar(select(ApplicationRecord).where(ApplicationRecord.job_id == job_id))
            if application and application.status == "needs_clarification":
                application.status = "waiting_for_approval"
            for record in records:
                session.expunge(record)
            return True, records

    def has_open_required_clarifications(self, job_id: int) -> bool:
        with Session(self.engine) as session:
            return session.scalar(select(ClarificationRequestRecord.id).where(
                ClarificationRequestRecord.job_id == job_id,
                ClarificationRequestRecord.required.is_(True),
                ClarificationRequestRecord.state != "resolved",
            ).limit(1)) is not None

    def resolved_vacancy_answers(self, job_id: int) -> dict[str, str]:
        """Return only per-vacancy answers; profile-scoped answers live in YAML."""
        with Session(self.engine) as session:
            records = list(session.scalars(select(ClarificationRequestRecord).where(
                ClarificationRequestRecord.job_id == job_id,
                ClarificationRequestRecord.answer_scope == "vacancy",
                ClarificationRequestRecord.state == "resolved",
            )))
            return {record.field_name: record.answer for record in records if record.field_name and record.answer}

    def get_job_id_by_external(self, site: str, external_job_id: str) -> int | None:
        with Session(self.engine) as session:
            item = session.scalar(select(JobRecord).where(JobRecord.site == site, JobRecord.external_job_id == external_job_id))
            return item.id if item else None

    def import_external_application(self, site: str, external_job_id: str, status: str, detail: str) -> bool:
        """Create a local tracker row only for a known local vacancy, never a guessed one."""
        job_id = self.get_job_id_by_external(site, external_job_id)
        if job_id is None:
            return False
        try:
            application = self.get_application_for_job(job_id)
        except ValueError:
            application = self.save_draft(job_id, site, "[Imported from confirmed site response list]")
            self.confirm_application_submission(job_id, external_job_id, "Imported from confirmed site response list")
        self.set_application_status(application.id, status, "site_import", detail)
        return True

    def record_confirmed_external_application(
        self,
        site: str,
        external_job_id: str,
        final_text: str,
        confirmation_detail: str,
    ) -> ApplicationRecord:
        """Persist an application only after a human or adapter observed site confirmation.

        This is intentionally local-only: it creates no browser session and sends no data.
        The vacancy must already be present in the local tracker, so an accidental typo
        cannot manufacture an application against an unknown listing.
        """
        job_id = self.get_job_id_by_external(site, external_job_id)
        if job_id is None:
            raise ValueError(f"No tracked {site} vacancy with external ID {external_job_id}")
        try:
            application = self.get_application_for_job(job_id)
        except ValueError:
            application = self.save_draft(job_id, site, final_text)
        self.set_application_final_text(application.id, final_text)
        current = self.get_application(application.id)
        if current.status != "submitted":
            self.confirm_application_submission(job_id, external_job_id, confirmation_detail)
        return self.get_application(application.id)

    def save_draft(self, job_id: int, site: str, text: str) -> ApplicationRecord:
        with Session(self.engine) as session, session.begin():
            app = session.scalar(select(ApplicationRecord).where(ApplicationRecord.job_id == job_id))
            if app is None:
                app = ApplicationRecord(job_id=job_id, site=site, draft=text, created_at=utcnow())
                session.add(app)
                session.flush()
            else: app.draft = text
            job = session.get(JobRecord, job_id)
            if job and job.status != "needs_clarification":
                job.status = "draft_created"
            session.expunge(app)
            return app

    def set_application_final_text(self, application_id: int, text: str) -> None:
        with Session(self.engine) as session, session.begin():
            record = session.get(ApplicationRecord, application_id)
            if record is None:
                raise ValueError(f"Unknown application ID: {application_id}")
            record.final_text = text

    def set_offer_details(self, application_id: int, title: str, price: str, duration: str) -> None:
        """Store an actual marketplace offer, separately from its cover text."""
        with Session(self.engine) as session, session.begin():
            record = session.get(ApplicationRecord, application_id)
            if record is None:
                raise ValueError(f"Unknown application ID: {application_id}")
            record.offer_title, record.offer_price, record.offer_duration = title, price, duration

    def set_application_status(self, application_id: int, status: str, source: str, detail: str = "") -> None:
        with Session(self.engine) as session, session.begin():
            application = session.get(ApplicationRecord, application_id)
            if application is None:
                raise ValueError(f"Unknown application ID: {application_id}")
            previous = application.status
            if previous == status:
                return
            application.status = status
            if status == "submitted":
                application.submitted_at = utcnow()
            if status in self._ACTIVE_APPLICATION_STATUSES:
                job = session.get(JobRecord, application.job_id)
                if job and job.status not in {"closed", "expired", "ignored"}:
                    job.status, job.checked_at = "applied", utcnow()
            session.add(ApplicationStatusHistoryRecord(application_id=application_id, previous_status=previous, new_status=status, source=source, detail=detail, created_at=utcnow()))

    def get_application_for_job(self, job_id: int) -> ApplicationRecord:
        with Session(self.engine) as session:
            record = session.scalar(select(ApplicationRecord).where(ApplicationRecord.job_id == job_id))
            if record is None:
                raise ValueError(f"No draft exists for job #{job_id}")
            session.expunge(record)
            return record

    def confirm_application_submission(self, job_id: int, external_application_id: str | None, detail: str) -> None:
        application = self.get_application_for_job(job_id)
        with Session(self.engine) as session, session.begin():
            record = session.get(ApplicationRecord, application.id)
            if record is None:
                raise ValueError(f"No draft exists for job #{job_id}")
            previous = record.status
            record.status, record.submitted_at = "submitted", utcnow()
            record.external_application_id = external_application_id
            job = session.get(JobRecord, job_id)
            if job and job.status not in {"closed", "expired", "ignored"}:
                job.status, job.checked_at = "applied", utcnow()
            session.add(ApplicationStatusHistoryRecord(application_id=record.id, previous_status=previous, new_status="submitted", source="site_confirmation", detail=detail, created_at=utcnow()))

    def list_applications(self) -> list[ApplicationRecord]:
        with Session(self.engine) as session:
            records = list(session.scalars(select(ApplicationRecord).order_by(ApplicationRecord.created_at.desc())))
            for record in records:
                session.expunge(record)
            return records

    def get_application(self, application_id: int) -> ApplicationRecord:
        with Session(self.engine) as session:
            record = session.get(ApplicationRecord, application_id)
            if record is None:
                raise ValueError(f"Unknown application ID: {application_id}")
            session.expunge(record)
            return record

    def list_application_history(self, application_id: int) -> list[ApplicationStatusHistoryRecord]:
        with Session(self.engine) as session:
            records = list(session.scalars(
                select(ApplicationStatusHistoryRecord)
                .where(ApplicationStatusHistoryRecord.application_id == application_id)
                .order_by(ApplicationStatusHistoryRecord.created_at)
            ))
            for record in records:
                session.expunge(record)
            return records

    def set_application_status_by_external(self, site: str, external_application_id: str, status: str, detail: str = "") -> bool:
        with Session(self.engine) as session, session.begin():
            record = session.scalar(
                select(ApplicationRecord).where(
                    ApplicationRecord.site == site,
                    ApplicationRecord.external_application_id == external_application_id,
                )
            )
            if record is None or record.status == status:
                return False
            previous = record.status
            record.status = status
            if status in self._ACTIVE_APPLICATION_STATUSES:
                job = session.get(JobRecord, record.job_id)
                if job and job.status not in {"closed", "expired", "ignored"}:
                    job.status, job.checked_at = "applied", utcnow()
            session.add(ApplicationStatusHistoryRecord(application_id=record.id, previous_status=previous, new_status=status, source="site_status", detail=detail, created_at=utcnow()))
            return True

    def list_messages(self, unread_only: bool = False) -> list[MessageRecord]:
        with Session(self.engine) as session:
            stmt = select(MessageRecord).order_by(MessageRecord.received_at.desc())
            if unread_only:
                stmt = stmt.where(MessageRecord.is_unread)
            records = list(session.scalars(stmt))
            for record in records:
                session.expunge(record)
            return records

    def list_conversations(self) -> list[tuple[str, str, int, datetime]]:
        """Return local conversation summaries without opening any remote chat."""
        with Session(self.engine) as session:
            messages = list(session.scalars(select(MessageRecord).order_by(MessageRecord.received_at.desc())))
            summaries: dict[tuple[str, str], tuple[str, int, datetime]] = {}
            for message in messages:
                key = (message.site, message.conversation_id)
                if key not in summaries:
                    summaries[key] = (message.body, 1 if message.is_unread else 0, message.received_at)
                else:
                    body, unread, received = summaries[key]
                    summaries[key] = (body, unread + int(message.is_unread), received)
            return [
                (f"{site}:{conversation}", body, unread, received)
                for (site, conversation), (body, unread, received) in sorted(summaries.items(), key=lambda item: item[1][2], reverse=True)
            ]

    def list_conversation_messages(self, site: str, conversation_id: str) -> list[MessageRecord]:
        with Session(self.engine) as session:
            records = list(session.scalars(
                select(MessageRecord)
                .where(MessageRecord.site == site, MessageRecord.conversation_id == conversation_id)
                .order_by(MessageRecord.received_at)
            ))
            for record in records:
                session.expunge(record)
            return records

    def get_message(self, message_id: int) -> MessageRecord:
        with Session(self.engine) as session:
            record = session.get(MessageRecord, message_id)
            if record is None:
                raise ValueError(f"Unknown message ID: {message_id}")
            session.expunge(record)
            return record

    def save_message_reply(self, message_id: int, draft: str, status: str = "draft", reason: str = "") -> MessageReplyRecord:
        """Persist a local reply decision without touching a remote conversation."""
        with Session(self.engine) as session, session.begin():
            message = session.get(MessageRecord, message_id)
            if message is None:
                raise ValueError(f"Unknown message ID: {message_id}")
            record = session.scalar(select(MessageReplyRecord).where(MessageReplyRecord.message_id == message_id))
            if record is None:
                record = MessageReplyRecord(
                    message_id=message.id,
                    site=message.site,
                    conversation_id=message.conversation_id,
                    draft=draft,
                    status=status,
                    reason=reason,
                    created_at=utcnow(),
                )
                session.add(record)
                session.flush()
            elif record.status != "sent":
                record.draft, record.status, record.reason = draft, status, reason
            session.expunge(record)
            return record

    def get_message_reply(self, message_id: int) -> MessageReplyRecord:
        with Session(self.engine) as session:
            record = session.scalar(select(MessageReplyRecord).where(MessageReplyRecord.message_id == message_id))
            if record is None:
                raise ValueError(f"No reply draft exists for message #{message_id}")
            session.expunge(record)
            return record

    def get_message_context(self, message_id: int) -> dict[str, object]:
        """Return an incoming message together with its one local reply state."""
        message = self.get_message(message_id)
        try:
            reply = self.get_message_reply(message_id)
            reply_data: dict[str, object] | None = {
                "status": reply.status,
                "draft": reply.final_text or reply.draft,
                "reason": reply.reason,
                "confirmation": reply.confirmation_detail,
            }
        except ValueError:
            reply_data = None
        return {
            "message": {
                "id": message.id,
                "site": message.site,
                "sender": message.sender,
                "category": message.category,
                "body": message.body,
                "conversation_id": message.conversation_id,
                "job_id": message.job_id,
            },
            "reply": reply_data,
        }

    def list_message_replies(self, status: str | None = None) -> list[MessageReplyRecord]:
        with Session(self.engine) as session:
            stmt = select(MessageReplyRecord).order_by(MessageReplyRecord.created_at.desc())
            if status:
                stmt = stmt.where(MessageReplyRecord.status == status)
            records = list(session.scalars(stmt))
            for record in records:
                session.expunge(record)
            return records

    def confirm_message_reply_sent(self, message_id: int, final_text: str, detail: str) -> None:
        with Session(self.engine) as session, session.begin():
            record = session.scalar(select(MessageReplyRecord).where(MessageReplyRecord.message_id == message_id))
            if record is None:
                raise ValueError(f"No reply draft exists for message #{message_id}")
            record.final_text = final_text
            record.status, record.confirmation_detail, record.sent_at = "sent", detail, utcnow()

    def upsert_message(self, raw: RawMessage) -> tuple[MessageRecord, bool]:
        with Session(self.engine) as session, session.begin():
            record = session.scalar(select(MessageRecord).where(MessageRecord.site == raw.site, MessageRecord.external_message_id == raw.external_message_id))
            # For unread inboxes, retain the canonical conversation item even
            # when a site changed only its preview markup/badge around the same
            # latest message. A reply draft always wins over a transient copy.
            if raw.is_unread:
                active = list(session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.site == raw.site,
                        MessageRecord.conversation_id == raw.conversation_id,
                        MessageRecord.is_unread.is_(True),
                    )
                    .order_by(MessageRecord.received_at)
                ))
                if active:
                    reply_ids = set(session.scalars(
                        select(MessageReplyRecord.message_id).where(
                            MessageReplyRecord.message_id.in_([item.id for item in active])
                        )
                    ))
                    canonical = next((item for item in active if item.id in reply_ids), active[0])
                    if record is not None and record.id != canonical.id:
                        record.is_unread = False
                    record = canonical
            created = record is None
            if record is None:
                record = MessageRecord(site=raw.site, external_message_id=raw.external_message_id, conversation_id=raw.conversation_id, sender=raw.sender, body=raw.body, category=raw.category, is_unread=raw.is_unread, received_at=raw.received_at or utcnow())
                session.add(record)
                session.flush()
            else:
                record.sender, record.body = raw.sender, raw.body
                record.category, record.is_unread = raw.category, raw.is_unread
            session.expunge(record)
            return record, created

    def start_run(self, kind: str, site: str | None = None) -> RunRecord:
        with Session(self.engine) as session, session.begin():
            record = RunRecord(kind=kind, site=site, status="running", started_at=utcnow())
            session.add(record)
            session.flush()
            session.expunge(record)
            return record

    def start_agent_session(self, subject_type: str, subject_id: int | None, mode: str) -> AgentSessionRecord:
        with Session(self.engine) as session, session.begin():
            record = AgentSessionRecord(subject_type=subject_type, subject_id=subject_id, mode=mode, started_at=utcnow())
            session.add(record)
            session.flush()
            session.expunge(record)
            return record

    def finish_agent_session(self, session_id: int, status: str, summary: str = "") -> None:
        with Session(self.engine) as session, session.begin():
            record = session.get(AgentSessionRecord, session_id)
            if record:
                record.status, record.summary, record.finished_at = status, summary[:4000], utcnow()

    def add_agent_event(
        self,
        session_id: int,
        subject_type: str,
        subject_id: int | None,
        event_type: str,
        tool_name: str = "",
        access_level: str = "read",
        intent: str = "",
        payload: dict[str, object] | None = None,
        detail: str = "",
    ) -> AgentEventRecord:
        with Session(self.engine) as session, session.begin():
            record = AgentEventRecord(
                session_id=session_id,
                subject_type=subject_type,
                subject_id=subject_id,
                event_type=event_type,
                tool_name=tool_name,
                access_level=access_level,
                intent=intent[:1000],
                payload_json=json.dumps(payload or {}, ensure_ascii=False),
                detail=detail[:4000],
                created_at=utcnow(),
            )
            session.add(record)
            session.flush()
            session.expunge(record)
            return record

    def list_agent_events(self, subject_type: str | None = None, subject_id: int | None = None, limit: int = 100) -> list[AgentEventRecord]:
        with Session(self.engine) as session:
            # SQLite timestamps may share the same stored precision for a
            # burst of tool events. ``id`` is the append-only tie-breaker, so
            # conversational "latest context" remains deterministic.
            stmt = select(AgentEventRecord).order_by(AgentEventRecord.created_at.desc(), AgentEventRecord.id.desc()).limit(limit)
            if subject_type:
                stmt = stmt.where(AgentEventRecord.subject_type == subject_type)
            if subject_id is not None:
                stmt = stmt.where(AgentEventRecord.subject_id == subject_id)
            records = list(session.scalars(stmt))
            for record in records:
                session.expunge(record)
            return records

    def list_agent_sessions(self, limit: int = 50) -> list[AgentSessionRecord]:
        with Session(self.engine) as session:
            records = list(session.scalars(select(AgentSessionRecord).order_by(AgentSessionRecord.started_at.desc()).limit(limit)))
            for record in records:
                session.expunge(record)
            return records

    def finish_run(self, run_id: int, status: str, detail: str = "") -> None:
        with Session(self.engine) as session, session.begin():
            record = session.get(RunRecord, run_id)
            if record:
                record.status, record.detail, record.finished_at = status, detail, utcnow()

    def fail_stale_running_runs(self, after: timedelta = timedelta(minutes=10)) -> int:
        """Close interrupted run-history rows left behind by a stopped worker.

        Queue recovery makes the underlying task runnable again.  This companion
        cleanup keeps the history truthful: a row cannot remain ``running`` once
        its owning process has been gone longer than the recovery threshold.
        """
        cutoff = utcnow() - after
        detail = "Recovered after worker restart or timeout; the task was requeued when eligible."
        with Session(self.engine) as session, session.begin():
            records = list(session.scalars(
                select(RunRecord).where(RunRecord.status == "running", RunRecord.started_at < cutoff)
            ))
            for record in records:
                record.status, record.detail, record.finished_at = "interrupted", detail, utcnow()
            return len(records)

    def list_runs(self) -> list[RunRecord]:
        with Session(self.engine) as session:
            records = list(session.scalars(select(RunRecord).order_by(RunRecord.started_at.desc())))
            for record in records:
                session.expunge(record)
            return records

    def get_run(self, run_id: int) -> RunRecord:
        with Session(self.engine) as session:
            record = session.get(RunRecord, run_id)
            if record is None:
                raise ValueError(f"Unknown run ID: {run_id}")
            session.expunge(record)
            return record

    def enqueue_task(self, kind: str, site: str | None, payload: dict[str, object], idempotency_key: str, priority: int = 0) -> QueueTaskRecord:
        with Session(self.engine) as session, session.begin():
            existing = session.scalar(select(QueueTaskRecord).where(QueueTaskRecord.idempotency_key == idempotency_key))
            if existing is not None:
                # Idempotency deduplicates concurrent enqueue attempts, not all
                # future schedule cycles. Once the previous execution reached a
                # terminal state, reuse its durable row for the next run while
                # preserving the separately recorded run history.
                if existing.status in {"completed", "failed"}:
                    now = utcnow()
                    existing.kind, existing.site = kind, site
                    existing.payload_json, existing.priority = json.dumps(payload, ensure_ascii=False), priority
                    existing.status, existing.attempts, existing.run_after = "queued", 0, now
                    existing.updated_at, existing.last_error = now, ""
                    session.flush()
                session.expunge(existing)
                return existing
            now = utcnow()
            task = QueueTaskRecord(kind=kind, site=site, payload_json=json.dumps(payload, ensure_ascii=False), priority=priority, idempotency_key=idempotency_key, run_after=now, created_at=now, updated_at=now)
            session.add(task)
            session.flush()
            session.expunge(task)
            return task

    def list_tasks(self, status: str | None = None) -> list[QueueTaskRecord]:
        with Session(self.engine) as session:
            stmt = select(QueueTaskRecord).order_by(QueueTaskRecord.priority.desc(), QueueTaskRecord.run_after)
            if status:
                stmt = stmt.where(QueueTaskRecord.status == status)
            records = list(session.scalars(stmt))
            for record in records:
                session.expunge(record)
            return records

    def claim_next_task(self) -> QueueTaskRecord | None:
        with Session(self.engine) as session, session.begin():
            task = session.scalar(
                select(QueueTaskRecord)
                .where(QueueTaskRecord.status == "queued", QueueTaskRecord.run_after <= utcnow())
                .order_by(QueueTaskRecord.priority.desc(), QueueTaskRecord.run_after)
                .limit(1)
            )
            if task is None:
                return None
            task.status, task.attempts, task.updated_at = "running", task.attempts + 1, utcnow()
            session.flush()
            session.expunge(task)
            return task

    def requeue_stale_running_tasks(self, after: timedelta = timedelta(minutes=10)) -> int:
        """Return orphaned browser work to the durable queue after a crash/restart."""
        cutoff = utcnow() - after
        with Session(self.engine) as session, session.begin():
            tasks = list(session.scalars(
                select(QueueTaskRecord).where(
                    QueueTaskRecord.status == "running",
                    QueueTaskRecord.updated_at <= cutoff,
                )
            ))
            now = utcnow()
            for task in tasks:
                task.status, task.run_after, task.updated_at = "queued", now, now
                task.last_error = "Recovered stale running task after scheduler restart."
            return len(tasks)

    def finish_task(self, task_id: int, detail: str = "") -> None:
        with Session(self.engine) as session, session.begin():
            task = session.get(QueueTaskRecord, task_id)
            if task:
                task.status, task.last_error, task.updated_at = "completed", detail, utcnow()

    def retry_task(self, task_id: int, error: str) -> None:
        with Session(self.engine) as session, session.begin():
            task = session.get(QueueTaskRecord, task_id)
            if task is None:
                return
            task.last_error, task.updated_at = error[:4000], utcnow()
            if task.attempts >= task.max_attempts:
                task.status = "failed"
            else:
                task.status = "queued"
                task.run_after = utcnow() + timedelta(seconds=min(300, 2 ** task.attempts * 10))

    def stats(self) -> dict[str, int]:
        with Session(self.engine) as session:
            jobs = list(session.scalars(select(JobRecord)))
            apps = list(session.scalars(select(ApplicationRecord)))
            analyses = list(session.scalars(select(AnalysisRecord)))
            messages = list(session.scalars(select(MessageRecord)))
            clarifications = list(session.scalars(select(ClarificationRequestRecord)))
            clarification_jobs = {item.job_id for item in clarifications if item.required and item.state != "resolved"}
            return {"jobs": len(jobs), "analyzed": len(analyses), "score_70": sum(a.score >= 70 for a in analyses), "score_85": sum(a.score >= 85 for a in analyses), "drafts": len(apps), "submitted": sum(a.status in self._ACTIVE_APPLICATION_STATUSES for a in apps), "needs_clarification": len(clarification_jobs), "unread_messages": sum(message.is_unread for message in messages)}

    def submitted_today(self) -> int:
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with Session(self.engine) as session:
            return sum(
                1
                for item in session.scalars(select(ApplicationRecord).where(ApplicationRecord.status == "submitted"))
                if item.submitted_at and as_utc(item.submitted_at) >= today
            )

    def sent_messages_last_hour(self) -> int:
        cutoff = utcnow() - timedelta(hours=1)
        with Session(self.engine) as session:
            return sum(
                1 for item in session.scalars(select(MessageReplyRecord).where(MessageReplyRecord.status == "sent"))
                if item.sent_at and as_utc(item.sent_at) >= cutoff
            )

    def llm_cost_today(self) -> float:
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with Session(self.engine) as session:
            return float(sum(item.cost_usd for item in session.scalars(select(AnalysisRecord)) if as_utc(item.created_at) >= today))
