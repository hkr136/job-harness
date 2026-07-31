"""Vacancy normalization before matching, drafting and display."""

from __future__ import annotations

import json
import re

from job_agent.llm.prompts import get_system_prompt
from job_agent.llm.providers import LLMProvider
from job_agent.models import NormalizedVacancy, RawJobDetails


def normalize_locally(job: RawJobDetails) -> NormalizedVacancy:
    """Safe fallback when no normalization model is configured."""
    text = re.sub(r"\s+", " ", job.description).strip()
    sentences = [part.strip(" •-—") for part in re.split(r"(?<=[.!?])\s+|\n+", job.description) if part.strip()]
    return NormalizedVacancy(
        title=job.title,
        summary=(sentences[0] if sentences else text)[:700],
        responsibilities=sentences[1:5],
        requirements=list(job.requirements),
        stack=list(job.desired_skills),
        budget=job.budget,
        work_format=job.work_format,
    )


async def normalize_with_provider(provider: LLMProvider, job: RawJobDetails) -> NormalizedVacancy:
    text = await provider.complete(
        get_system_prompt("normalization"),
        json.dumps({"raw_listing": job.model_dump(mode="json"), "schema": NormalizedVacancy.model_json_schema()}, ensure_ascii=False),
        json_mode=True,
    )
    return NormalizedVacancy.model_validate_json(text)
