from __future__ import annotations

import asyncio
import json

from job_agent.browser.manager import BrowserManager
from job_agent.config.settings import Settings, load_profile
from job_agent.database.repositories import Store
from job_agent.llm.factory import provider_for_role, role_is_automated
from job_agent.models import SearchFilters
from job_agent.services.auto_workflow import AutoWorkflowService
from job_agent.services.message_service import MessageService
from job_agent.services.orchestration import OrchestrationService
from job_agent.services.search_service import SearchService
from job_agent.services.status_service import StatusService
from job_agent.sites.registry import build_adapter


class HarnessWorker:
    """Executes one durable task at a time; external actions stay adapter-owned."""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store, self.settings = store, settings

    def enqueue_scan(self, site: str, priority: int = 0) -> int:
        task = self.store.enqueue_task("scan", site, {}, f"scan:{site}", priority)
        return task.id

    def enqueue_message_check(self, site: str, priority: int = 1) -> int:
        task = self.store.enqueue_task("messages", site, {}, f"messages:{site}", priority)
        return task.id

    def enqueue_status_check(self, site: str, priority: int = 1) -> int:
        task = self.store.enqueue_task("statuses", site, {}, f"statuses:{site}", priority)
        return task.id

    async def process_one(self) -> str | None:
        task = self.store.claim_next_task()
        if task is None:
            return None
        run = self.store.start_run(task.kind, task.site)
        try:
            detail = await asyncio.wait_for(
                self._execute_task(task.kind, task.site),
                timeout=self.settings.limits.remote_task_timeout_seconds,
            )
            self.store.finish_task(task.id, detail)
            self.store.finish_run(run.id, "completed", detail)
            return detail
        except asyncio.TimeoutError:
            detail = f"{task.site or 'site'} {task.kind} timed out after {self.settings.limits.remote_task_timeout_seconds}s; retry {task.attempts}/{task.max_attempts}. Check ~/.job-harness/artifacts/playwright-traces."
            self.store.retry_task(task.id, detail)
            self.store.finish_run(run.id, "failed", detail)
            return f"failed: {detail}"
        except Exception as error:
            self.store.retry_task(task.id, str(error))
            self.store.finish_run(run.id, "failed", str(error))
            return f"failed: {error}"

    async def run_now(self, kind: str, site: str) -> str:
        """Execute one user-requested task immediately and record its run.

        This intentionally bypasses the durable queue and working-hours gate,
        just like an explicit CLI scan. It is used by the TUI's manual actions.
        """
        run = self.store.start_run(kind, site)
        try:
            detail = await asyncio.wait_for(
                self._execute_task(kind, site),
                timeout=self.settings.limits.remote_task_timeout_seconds,
            )
            self.store.finish_run(run.id, "completed", detail)
            return detail
        except Exception as error:
            detail = f"{site} {kind} timed out after {self.settings.limits.remote_task_timeout_seconds}s; check ~/.job-harness/artifacts/playwright-traces." if isinstance(error, asyncio.TimeoutError) else str(error)
            self.store.finish_run(run.id, "failed", detail)
            raise

    async def _execute_task(self, kind: str, site: str | None) -> str:
        """Perform one remote operation under the caller's single task deadline."""
        if kind not in {"scan", "messages", "statuses"} or not site:
            raise ValueError(f"Unsupported task kind: {kind}")
        config = self.settings.sites[site]
        browser = BrowserManager(
            self.settings.env.headless,
            self.settings.env.browser_min_action_delay_seconds,
            self.settings.env.browser_max_action_delay_seconds,
        )
        adapter = build_adapter(config.adapter, browser, config.browser_profile)
        auth = await adapter.check_auth()
        if not auth.authenticated:
            raise RuntimeError(auth.detail or "Authentication required")
        if kind == "messages":
            if not adapter.capabilities.read_messages:
                return json.dumps({"skipped": "adapter has no message reader"}, ensure_ascii=False)
            message_service = MessageService(self.store, adapter)
            records = await message_service.collect_new_unread()
            prepared, needs_clarification = 0, 0
            if records:
                if self.settings.applications.auto_mode and self.settings.applications.auto_reply_messages:
                    for record in records:
                        decision = message_service.prepare_reply(record.id, load_profile())
                        prepared += decision.status == "draft"
                        needs_clarification += decision.status == "needs_clarification"
                        # The adapter is the only route to a platform inbox. A
                        # Kwork adapter cannot be coerced into external contact.
                        if decision.status == "draft" and self.store.sent_messages_last_hour() < self.settings.limits.messages_per_hour:
                            await message_service.send_prepared_reply(record.id, confirm=True)
                else:
                    orchestrator = OrchestrationService(
                        self.store, self.settings, mode="safe", use_llm=role_is_automated(self.settings, "orchestration")
                    )
                    for record in records:
                        decision_text = await orchestrator.handle_message(record.id)
                        prepared += "Reply decision: draft" in decision_text
                        needs_clarification += "needs_clarification" in decision_text
            # ``new_messages`` means new to the local tracker, not necessarily
            # new to the user. Keep already-known unread threads visible in the
            # receipt; otherwise a second check falsely says "nothing here"
            # while the inbox still has open conversations.
            unread = [item for item in self.store.list_messages(unread_only=True) if item.site == site]
            actionable: list[dict[str, object]] = []
            tracked: list[dict[str, object]] = []
            for item in unread:
                try:
                    reply = self.store.get_message_reply(item.id)
                    reply_status = reply.status
                except ValueError:
                    reply_status = "unreviewed"
                tracked.append({"id": item.id, "sender": item.sender, "category": item.category, "reply_status": reply_status})
                if reply_status not in {"sent", "not_needed"}:
                    actionable.append({"id": item.id, "sender": item.sender, "category": item.category, "reply_status": reply_status})
            return json.dumps(
                {
                    "new_messages": len(records),
                    "reply_drafts": prepared,
                    "reply_needs_clarification": needs_clarification,
                    "known_unread": len(unread),
                    "tracked": tracked,
                    "actionable": actionable,
                },
                ensure_ascii=False,
            )
        if kind == "statuses":
            if not adapter.capabilities.application_statuses:
                return json.dumps({"skipped": "adapter has no status reader"}, ensure_ascii=False)
            changed = await StatusService(self.store, adapter).sync()
            return json.dumps({"changed_statuses": changed}, ensure_ascii=False)
        analyzer = provider_for_role(self.settings, "analysis") if role_is_automated(self.settings, "analysis") else None
        if analyzer is None and self.settings.env.llm_api_key:
            from job_agent.analysis.llm import LLMAnalyzer
            analyzer = LLMAnalyzer(self.settings.env.llm_api_key, self.settings.env.llm_base_url, self.settings.env.llm_model, self.settings.env.llm_temperature, self.settings.env.llm_max_tokens, self.settings.env.llm_input_cost_per_million_usd, self.settings.env.llm_output_cost_per_million_usd)
        normalizer = provider_for_role(self.settings, "normalization")
        service = SearchService(self.store, adapter, self.settings.matching.thresholds, analyzer, self.settings.limits.llm_budget_per_day_usd, normalizer)
        new, analyzed = await service.scan(SearchFilters.model_validate(config.search))
        auto_results: list[dict[str, object]] = []
        workflow = AutoWorkflowService(self.store, self.settings, adapter)
        if workflow.enabled:
            candidates = [job_id for job_id in service.last_created_job_ids]
            for job, analysis in self.store.list_jobs(min_score=self.settings.applications.auto_match_threshold):
                if job.status in {"analyzed", "draft_created", "ready_to_apply"} and analysis and job.id not in candidates:
                    candidates.append(job.id)
            for job_id in candidates:
                auto_results.append({"id": job_id, "result": await workflow.process_job(job_id)})
        elif service.last_created_job_ids and role_is_automated(self.settings, "orchestration"):
            orchestrator = OrchestrationService(self.store, self.settings, mode="safe")
            for job_id in service.last_created_job_ids:
                await orchestrator.handle_job(job_id)
        found: list[dict[str, object]] = []
        for job_id in service.last_scanned_job_ids[:20]:
            job, analysis = self.store.get_job(job_id)
            found.append({"id": job.id, "title": job.title, "score": analysis.match_score if analysis else None, "status": job.status})
        return json.dumps(
            {"new": new, "analyzed": analyzed, "found": found, "auto": auto_results, "skipped": len(service.last_errors), "errors": service.last_errors[:5]},
            ensure_ascii=False,
        )

    def process_one_sync(self) -> str | None:
        return asyncio.run(self.process_one())
