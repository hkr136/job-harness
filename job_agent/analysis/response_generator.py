from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from job_agent.llm.prompts import get_system_prompt
from job_agent.llm.providers import LLMProvider
from job_agent.models import AnalysisResult, RawJobDetails


class ApplicationReview(BaseModel):
    score: int = Field(ge=0, le=100)
    approved: bool = False
    reasons: list[str] = Field(default_factory=list)
    rewrite_notes: str = ""


def build_draft(job: RawJobDetails, analysis: AnalysisResult) -> str:
    claims = ", ".join(analysis.safe_claims_for_application[:4]) or "релевантных навыках и практическом опыте"
    project = analysis.best_portfolio_project or "релевантный проект"
    question = analysis.questions_for_client[0] if analysis.questions_for_client else "Каким будет первый ожидаемый результат?"
    return f"Здравствуйте! Задача по {job.title} близка моему опыту в {claims}. В похожем проекте ({project}) я собирал рабочий сценарий от интеграций до запуска и документации. Готов быстро разобрать текущий процесс и предложить безопасный первый этап. Подскажите, пожалуйста: {question}"


async def build_draft_with_provider(
    provider: LLMProvider, job: RawJobDetails, analysis: AnalysisResult, profile: dict[str, Any]
) -> str:
    """Generate a first-contact draft from runtime evidence only; it never sends it."""
    system = get_system_prompt("writing")
    user = json.dumps(
        {"vacancy": job.model_dump(mode="json"), "analysis": analysis.model_dump(mode="json"), "candidate_profile": profile},
        ensure_ascii=False,
    )
    return (await provider.complete(system, user)).strip()


async def review_application(
    provider: LLMProvider, job: RawJobDetails, analysis: AnalysisResult, profile: dict[str, Any], draft: str,
) -> ApplicationReview:
    payload = {
        "vacancy": job.model_dump(mode="json"), "analysis": analysis.model_dump(mode="json"),
        "candidate_profile": profile, "application_draft": draft,
        "schema": ApplicationReview.model_json_schema(),
    }
    text = await provider.complete(get_system_prompt("application_review"), json.dumps(payload, ensure_ascii=False), json_mode=True)
    return ApplicationReview.model_validate_json(text)
