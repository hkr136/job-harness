"""Persisted, idempotent unattended discover-to-confirm workflow."""

from __future__ import annotations

from job_agent.analysis.response_generator import (
    build_draft,
    build_draft_with_provider,
    review_application,
)
from job_agent.config.settings import Settings, load_profile
from job_agent.database.repositories import Store
from job_agent.llm.factory import provider_for_role
from job_agent.models import AnalysisResult, PreparedApplication, RawJobDetails
from job_agent.services.application_service import ApplicationService
from job_agent.services.recovery_service import (
    adapter_test_target,
    apply_verified_adapter_recovery,
    is_recoverable_adapter_error,
)
from job_agent.sites.base import BaseSiteAdapter


class AutoWorkflowService:
    """Runs only when persisted Auto mode is enabled, never from TUI ARM state."""

    def __init__(self, store: Store, settings: Settings, adapter: BaseSiteAdapter) -> None:
        self.store, self.settings, self.adapter = store, settings, adapter

    @property
    def enabled(self) -> bool:
        policy = self.settings.applications
        return bool(policy.auto_mode or policy.unattended_submission)

    @staticmethod
    def _raw(job) -> RawJobDetails:  # type: ignore[no-untyped-def]
        return RawJobDetails(external_job_id=job.external_job_id, site=job.site, url=job.url, title=job.title, company=job.company, budget=job.budget, work_format=job.work_format, published_at=job.published_at, description=job.normalized_text or job.description, summary=job.normalized_text or job.description)

    def _hard_gate(self, job, analysis: AnalysisResult) -> str | None:  # type: ignore[no-untyped-def]
        policy = self.settings.applications
        if analysis.match_score < policy.auto_match_threshold:
            return f"score {analysis.match_score} < auto threshold {policy.auto_match_threshold}"
        if analysis.critical_requirements:
            return "critical requirements are not confirmed"
        if job.work_format and not any(word in job.work_format.casefold() for word in ("remote", "удал", "гибрид")):
            return "work format is not remote"
        if self.store.has_open_required_clarifications(job.id):
            return "open clarifications"
        return None

    def _usage(self, role: str, action: str, provider, job_id: int, result: str = "completed") -> None:  # type: ignore[no-untyped-def]
        tokens = getattr(provider, "last_tokens", None)
        reported = tokens if tokens and str(getattr(provider, "provider_id", "")) != "codex" else None
        self.store.save_llm_usage(role=role, action=action, provider=str(getattr(provider, "provider_id", "unknown")), model=getattr(provider, "model", None), subject_type="job", subject_id=job_id, total_tokens=reported, cost_usd=(getattr(provider, "last_cost_usd", None) if reported is not None else None), result=result)

    async def process_job(self, job_id: int) -> str:
        if not self.enabled:
            return "auto mode disabled"
        job, analysis = self.store.get_job(job_id)
        if analysis is None:
            return "not analyzed"
        if job.status in {"applied", "closed", "expired", "ignored"}:
            return f"already {job.status}"
        if blocked := self._hard_gate(job, analysis):
            return f"blocked: {blocked}"
        raw, profile = self._raw(job), load_profile()
        try:
            application = self.store.get_application_for_job(job_id)
            draft = application.final_text or application.draft
        except ValueError:
            writer = provider_for_role(self.settings, "writing")
            if writer is None:
                draft = build_draft(raw, analysis)
            else:
                try:
                    draft = await build_draft_with_provider(writer, raw, analysis, profile)
                    self._usage("writing", "create_application_draft", writer, job_id)
                except Exception:
                    self._usage("writing", "create_application_draft", writer, job_id, "failed")
                    return "draft generation failed"
            application = self.store.save_draft(job_id, job.site, draft)
        # A dedicated role can be configured in Settings. Until then, reuse
        # the already-selected writing model with the separate review prompt;
        # this keeps a new profile's Auto mode functional without silently
        # choosing an unrelated provider.
        reviewer = provider_for_role(self.settings, "application_review") or provider_for_role(self.settings, "writing")
        if reviewer is None:
            return "blocked: application_review model is not configured"
        policy = self.settings.applications
        approved = False
        for attempt in range(policy.auto_max_rewrite_attempts + 1):
            try:
                review = await review_application(reviewer, raw, analysis, profile, draft)
                self._usage("application_review", "review_application", reviewer, job_id)
            except Exception:
                self._usage("application_review", "review_application", reviewer, job_id, "failed")
                return "review failed"
            approved = review.approved and review.score >= policy.auto_review_threshold
            self.store.save_application_review(application.id, job_id, score=review.score, approved=approved, reasons=review.reasons, rewrite_notes=review.rewrite_notes, provider=str(getattr(reviewer, "provider_id", "unknown")), model=getattr(reviewer, "model", None))
            if approved:
                break
            if attempt == policy.auto_max_rewrite_attempts:
                return "review did not pass after rewrite attempts"
            writer = provider_for_role(self.settings, "writing")
            if writer is None:
                return f"review {review.score}/100 did not pass; no writing model for rewrite"
            try:
                draft = await build_draft_with_provider(writer, raw, analysis, profile)
                self._usage("writing", "rewrite_application", writer, job_id)
                self.store.set_application_final_text(application.id, draft)
            except Exception:
                self._usage("writing", "rewrite_application", writer, job_id, "failed")
                return "rewrite failed"
        if not approved:
            return "review did not pass"
        prepared = PreparedApplication(job_id=job.id, site=job.site, body=draft, external_job_id=job.external_job_id)
        result = await ApplicationService(self.store, self.adapter, self.settings.limits.applications_per_day, policy.auto_match_threshold).submit(prepared, analysis, dry_run=False)
        if result.success and result.confirmed:
            return "submitted"
        if is_recoverable_adapter_error(result.detail):
            recovery = provider_for_role(self.settings, "recovery") or provider_for_role(self.settings, "writing")
            if recovery:
                fixed, artifact, detail = await apply_verified_adapter_recovery(recovery, self.adapter, job.site, result.detail, test_target=adapter_test_target(job.site))
                self._usage("recovery", "repair_adapter", recovery, job_id, "completed" if fixed else "failed")
                return f"not confirmed: {result.detail}; recovery {'activated' if fixed else 'failed'}: {artifact} · {detail}"
        return f"not confirmed: {result.detail}"
