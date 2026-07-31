"""Safe, user-profile-driven Kwork offer parameters."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from job_agent.llm.prompts import get_system_prompt
from job_agent.llm.providers import LLMProvider
from job_agent.models import AnalysisResult, RawJobDetails


class KworkOffer(BaseModel):
    title: str = Field(min_length=3, max_length=70)
    price: str = Field(pattern=r"^[\d\s]+$")
    duration: str = Field(pattern=r"^\d{1,3}$")
    body: str = Field(min_length=150, max_length=2000)


def _policy(profile: dict[str, Any]) -> dict[str, int]:
    rules = profile.get("candidate", {}).get("application_rules", {})
    supplied = rules.get("kwork_offer", {}) if isinstance(rules, dict) else {}
    # Platform-level defaults, not candidate facts. Candidate-specific values
    # can be set exclusively in profile.yaml.
    defaults = {"audit_price": 2500, "audit_days": 2, "small_price": 5000, "small_days": 3, "large_price": 15000, "large_days": 7}
    return {key: max(1, int(supplied.get(key, value))) for key, value in defaults.items()}


def _budget_amount(value: str | None) -> int | None:
    values = [int(item.replace(" ", "")) for item in re.findall(r"\d[\d\s]{0,11}", value or "")]
    return max(values) if values else None


def _tier(job: RawJobDetails, analysis: AnalysisResult) -> str:
    text = f"{job.title} {job.description}".casefold()
    if any(word in text for word in ("аудит", "диагност", "консультац", "промпт")):
        return "audit"
    if analysis.estimated_complexity.casefold() in {"large", "high", "complex"} or len(job.description) > 2200:
        return "large"
    return "small"


def build_kwork_offer(job: RawJobDetails, analysis: AnalysisResult, profile: dict[str, Any], body: str) -> KworkOffer:
    policy, tier = _policy(profile), _tier(job, analysis)
    floor, days = policy[f"{tier}_price"], policy[f"{tier}_days"]
    budget = _budget_amount(job.budget) or _budget_amount(job.description)
    # A buyer's low budget never forces the candidate to underprice work. When
    # it is adequate, retain the stated amount as the proposed fixed price.
    price = budget if budget is not None and budget >= floor else floor
    title = re.sub(r"\s+", " ", f"Решение задачи: {job.title}").strip()[:70]
    text = body.strip()
    if len(text) < 150:
        text = (text + " Сначала уточню текущий процесс, предложу понятный первый этап и после согласования реализую решение с проверкой результата.").strip()
    return KworkOffer(title=title, price=str(price), duration=str(days), body=text[:2000])


async def build_kwork_offer_with_provider(
    provider: LLMProvider, job: RawJobDetails, analysis: AnalysisResult, profile: dict[str, Any], body: str
) -> KworkOffer:
    baseline = build_kwork_offer(job, analysis, profile, body)
    system = get_system_prompt("writing") + (
        "\nFor this Kwork offer return strict JSON only with title, price, duration, body. "
        "Do not use external contacts. price must be digits only, duration must be a whole number of days, "
        "title no longer than 70 characters, body 150–2000 characters. Do not quote below the supplied price_floor."
    )
    user = json.dumps(
        {
            "vacancy": job.model_dump(mode="json"),
            "analysis": analysis.model_dump(mode="json"),
            "candidate_profile": profile,
            "existing_draft": body,
            "price_floor": int(baseline.price),
            "default_duration_days": baseline.duration,
            "fallback_offer": baseline.model_dump(),
        },
        ensure_ascii=False,
    )
    raw = (await provider.complete(system, user, json_mode=True)).strip()
    try:
        decoded = json.loads(raw.removeprefix("```json").removesuffix("```").strip())
        proposal = KworkOffer.model_validate(decoded)
        if int(proposal.price.replace(" ", "")) < int(baseline.price):
            proposal.price = baseline.price
        return proposal
    except Exception:
        # A model formatting error must not make us invent fields or cancel a
        # manual attempt; the transparent deterministic proposal remains valid.
        return baseline
