from __future__ import annotations

from job_agent.analysis.llm import analyze_with_provider
from job_agent.analysis.matcher import analyze_locally
from job_agent.analysis.normalizer import normalize_locally, normalize_with_provider
from job_agent.config.settings import load_profile
from job_agent.database.repositories import Store
from job_agent.llm.providers import LLMProvider
from job_agent.models import SearchFilters
from job_agent.sites.base import BaseSiteAdapter


class SearchService:
    def __init__(self, store: Store, adapter: BaseSiteAdapter, thresholds: dict[str, int], analyzer: LLMProvider | None = None, llm_daily_budget_usd: float | None = None, normalizer: LLMProvider | None = None) -> None:
        self.store, self.adapter, self.thresholds, self.analyzer = store, adapter, thresholds, analyzer
        self.llm_daily_budget_usd = llm_daily_budget_usd
        self.normalizer = normalizer
        self.last_errors: list[str] = []
        self.last_created_job_ids: list[int] = []
        self.last_scanned_job_ids: list[int] = []

    async def scan(self, filters: SearchFilters) -> tuple[int, int]:
        new, analyzed = 0, 0
        profile = load_profile()
        for details in await self.adapter.collect_job_details(filters):
            try:
                try:
                    normalized = await normalize_with_provider(self.normalizer, details) if self.normalizer else normalize_locally(details)
                    if self.normalizer:
                        provider_id = str(getattr(self.normalizer, "provider_id", "unknown"))
                        tokens = getattr(self.normalizer, "last_tokens", None)
                        self.store.save_llm_usage(
                            role="normalization", action="normalize_job", provider=provider_id,
                            model=getattr(self.normalizer, "model", None), subject_type="job",
                            total_tokens=tokens if tokens and provider_id != "codex" else None,
                            cost_usd=getattr(self.normalizer, "last_cost_usd", None) if tokens and provider_id != "codex" else None,
                        )
                except Exception:
                    normalized = normalize_locally(details)
                # Raw source stays in ``description``. The display and all
                # downstream reasoning use this explicit normalized record.
                details.normalized_text = "[normalized]\n" + normalized.as_text()
                details.requirements = normalized.requirements
                details.desired_skills = normalized.stack
                details.budget = normalized.budget or details.budget
                details.work_format = normalized.work_format or details.work_format
                analysis_details = details.model_copy(update={
                    "title": normalized.title or details.title,
                    "summary": normalized.summary or details.summary,
                    "description": normalized.as_text(),
                })
                needs_analysis = self.store.job_needs_analysis(details)
                record, created = self.store.upsert_job(details)
                self.last_scanned_job_ids.append(record.id)
                if created:
                    new += 1
                    self.last_created_job_ids.append(record.id)
                if not needs_analysis:
                    continue
                model, tokens, cost_usd = "local", 0, 0.0
                try:
                    use_llm = self.analyzer and (self.llm_daily_budget_usd is None or self.store.llm_cost_today() < self.llm_daily_budget_usd)
                    analysis = await analyze_with_provider(self.analyzer, analysis_details, profile) if use_llm else analyze_locally(analysis_details, profile, self.thresholds)
                    if use_llm:
                        model, tokens, cost_usd = self.analyzer.model, self.analyzer.last_tokens, self.analyzer.last_cost_usd
                except Exception:
                    analysis = analyze_locally(details, profile, self.thresholds)
                self.store.save_analysis(record.id, analysis, model=model, tokens=tokens, cost_usd=cost_usd)
                if use_llm and self.analyzer:
                    provider_id = str(getattr(self.analyzer, "provider_id", "legacy"))
                    # Codex's authenticated CLI has no token-usage field; NULL
                    # lets the UI state that honestly instead of inventing it.
                    reported = tokens if tokens and provider_id != "codex" else None
                    self.store.save_llm_usage(
                        role="analysis", action="analyze_job", provider=provider_id,
                        model=getattr(self.analyzer, "model", None), subject_type="job", subject_id=record.id,
                        total_tokens=reported, cost_usd=cost_usd if reported is not None else None,
                    )
                analyzed += 1
            except Exception as error:
                # A broken/deleted listing must not abort the rest of the site's batch.
                self.last_errors.append(f"{details.external_job_id}: {type(error).__name__}: {str(error)[:180]}")
                continue
        return new, analyzed
