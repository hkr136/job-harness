from __future__ import annotations

import json
from typing import Any

from job_agent.llm.prompts import get_system_prompt
from job_agent.llm.providers import LLMProvider, OpenAICompatibleProvider
from job_agent.models import AnalysisResult, RawJobDetails

SYSTEM_PROMPT = """Compatibility export; active text comes from the user prompt registry."""


class LLMAnalyzer:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 1600,
        input_cost_per_million_usd: float = 0.0,
        output_cost_per_million_usd: float = 0.0,
    ) -> None:
        self.provider = OpenAICompatibleProvider(
            "legacy", api_key, base_url, model,
            temperature=temperature,
            max_tokens=max_tokens,
            input_cost_per_million_usd=input_cost_per_million_usd,
            output_cost_per_million_usd=output_cost_per_million_usd,
        )
        self.model = model

    @property
    def last_tokens(self) -> int:
        return self.provider.last_tokens

    @property
    def last_cost_usd(self) -> float:
        return self.provider.last_cost_usd

    async def analyze(self, job: RawJobDetails, profile: dict[str, Any]) -> AnalysisResult:
        return await analyze_with_provider(self.provider, job, profile)


async def analyze_with_provider(provider: LLMProvider, job: RawJobDetails, profile: dict[str, Any]) -> AnalysisResult:
    text = await provider.complete(
        get_system_prompt("analysis"),
        json.dumps({"vacancy": job.model_dump(mode="json"), "candidate_profile": profile, "schema": AnalysisResult.model_json_schema()}, ensure_ascii=False),
        json_mode=True,
    )
    return AnalysisResult.model_validate_json(text)
