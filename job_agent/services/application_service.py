from __future__ import annotations

from job_agent.database.repositories import Store
from job_agent.models import AnalysisResult, PreparedApplication, SubmissionResult
from job_agent.sites.base import BaseSiteAdapter


class ApplicationService:
    """Single policy gate for irreversible application actions."""

    def __init__(self, store: Store, adapter: BaseSiteAdapter, daily_limit: int, auto_threshold: int) -> None:
        self.store, self.adapter = store, adapter
        self.daily_limit, self.auto_threshold = daily_limit, auto_threshold

    def eligible(self, analysis: AnalysisResult) -> tuple[bool, str]:
        sent_today = self.store.submitted_today()
        if sent_today >= self.daily_limit:
            return False, "Daily application limit reached"
        if not self.adapter.capabilities.submit_application:
            return False, "Site adapter does not support confirmed submission"
        if analysis.match_score < self.auto_threshold or analysis.critical_requirements:
            return False, "Application does not pass auto-apply policy"
        return True, "eligible"

    async def submit(
        self, prepared: PreparedApplication, analysis: AnalysisResult, dry_run: bool = True, *, manual: bool = False
    ) -> SubmissionResult:
        if self.store.has_open_required_clarifications(prepared.job_id):
            return SubmissionResult(success=False, confirmed=False, detail="Open required clarifications block submission")
        if manual:
            # An explicit, armed user action is a review decision. Automatic
            # score/limit gates must not silently veto it; platform capability,
            # required-profile answers and site confirmation remain mandatory.
            if not self.adapter.capabilities.submit_application:
                return SubmissionResult(success=False, confirmed=False, detail="Site adapter does not support confirmed submission")
        else:
            allowed, reason = self.eligible(analysis)
            if not allowed:
                return SubmissionResult(success=False, confirmed=False, detail=reason)
        if dry_run:
            return SubmissionResult(success=True, confirmed=False, detail="Dry-run: form would be prepared but not submitted")
        prepared = prepared.model_copy(update={
            "form_answers": {**prepared.form_answers, **self.store.resolved_vacancy_answers(prepared.job_id)},
        })
        result = await self.adapter.submit_application(prepared, confirm=True)
        if result.clarifications:
            try:
                application_id = self.store.get_application_for_job(prepared.job_id).id
            except ValueError:
                application_id = None
            self.store.create_clarifications(prepared.job_id, prepared.site, result.clarifications, application_id)
            return SubmissionResult(
                success=False,
                confirmed=False,
                detail="Required profile facts were saved to the clarification queue; no response was sent.",
                screenshot_path=result.screenshot_path,
                clarifications=result.clarifications,
            )
        if result.success and result.confirmed:
            self.store.confirm_application_submission(prepared.job_id, result.external_application_id, result.detail)
        return result
