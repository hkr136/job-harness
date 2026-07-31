from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from job_agent.database.repositories import Store
from job_agent.models import AnalysisResult


class StatisticsService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def funnel(self) -> dict[str, int]:
        jobs = self.store.list_jobs()
        apps = self.store.list_applications()
        statuses = Counter(app.status for app in apps)
        return {
            "found": len(jobs),
            "relevant": sum(analysis is not None and analysis.score >= 70 for _, analysis in jobs),
            "drafted": len(apps),
            "needs_clarification": sum(job.status == "needs_clarification" for job, _ in jobs),
            "sent": statuses["submitted"],
            "viewed": statuses["viewed"],
            "replied": statuses["client_replied"],
            "interview": statuses["interview"],
            "offer": statuses["accepted"],
        }

    def skills(self) -> dict[str, list[tuple[str, int]]]:
        matched: Counter[str] = Counter()
        missing: Counter[str] = Counter()
        for _, record in self.store.list_jobs():
            if record is None:
                continue
            analysis = AnalysisResult.model_validate_json(record.payload_json)
            matched.update(analysis.matched_skills)
            missing.update(analysis.missing_skills + analysis.critical_requirements)
        return {"matched": matched.most_common(15), "missing": missing.most_common(15)}

    def overview(self, since: datetime | None = None, site: str | None = None) -> dict[str, float | int]:
        """Compute metrics from persisted records; no counters are stored separately."""
        def after(value: datetime, boundary: datetime | None) -> bool:
            if boundary is None:
                return True
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value >= boundary

        jobs = self.store.list_jobs(site=site)
        if since:
            jobs = [(job, analysis) for job, analysis in jobs if after(job.discovered_at, since)]
        apps = [item for item in self.store.list_applications() if (not site or item.site == site) and after(item.created_at, since)]
        analyses = [record for _, record in jobs if record is not None]
        statuses = Counter(item.status for item in apps)
        submitted = statuses["submitted"] + statuses["viewed"] + statuses["client_replied"] + statuses["interview"] + statuses["accepted"] + statuses["rejected"]
        return {
            "found": len(jobs),
            "analyzed": len(analyses),
            "relevant": sum(item.score >= 70 for item in analyses),
            "high_priority": sum(item.score >= 85 for item in analyses),
            "drafted": len(apps),
            "submitted_or_later": submitted,
            "viewed": statuses["viewed"],
            "replied": statuses["client_replied"],
            "interview": statuses["interview"],
            "test_task": statuses["test_task"],
            "rejected": statuses["rejected"],
            "accepted": statuses["accepted"],
            "response_rate_percent": round(100 * (statuses["client_replied"] + statuses["interview"] + statuses["accepted"]) / submitted, 1) if submitted else 0.0,
            "average_score": round(sum(item.score for item in analyses) / len(analyses), 1) if analyses else 0.0,
            "llm_tokens": sum(item.tokens for item in analyses),
            "llm_cost_usd": round(sum(item.cost_usd for item in analyses), 6),
            "cost_per_analysis_usd": round(sum(item.cost_usd for item in analyses) / len(analyses), 6) if analyses else 0.0,
        }
