from __future__ import annotations

import re
from typing import Any

from job_agent.models import AnalysisResult, RawJobDetails


def _flat(profile: dict[str, Any], key: str) -> list[str]:
    candidate = profile.get("candidate", {})
    levels = candidate.get("skill_levels", {})
    value = levels.get(key, {})
    if isinstance(value, list): return value
    if key == "core": return [tech for group in value.values() for tech in group.get("technologies", [])]
    return []


def _portfolio_project(profile: dict[str, Any]) -> str:
    """Read either supported user-data shape without embedding personal content.

    Older profile files group entries under ``experience.projects``; the neutral
    example also permits a simple ``experience`` list. Both stay user-owned.
    """
    experience = profile.get("candidate", {}).get("experience", [])
    if isinstance(experience, dict):
        experience = experience.get("projects", [])
    if not isinstance(experience, list) or not experience:
        return "a relevant prior project"
    first = experience[0]
    if isinstance(first, dict):
        return str(first.get("name") or first.get("description") or "a relevant prior project")
    return str(first)


def _years_requirement(text: str) -> int | None:
    match = re.search(r"(?:от\s*)?(\d+)\s*(?:[–—-]\s*\d+\s*)?(?:лет|года|years?)", text, re.I)
    return int(match.group(1)) if match else None


def analyze_locally(job: RawJobDetails, profile: dict[str, Any], thresholds: dict[str, int]) -> AnalysisResult:
    text = f"{job.title} {job.description}".lower()
    core, extra, learning, not_claimed = (_flat(profile, key) for key in ("core", "additional_experience", "learning", "not_claimed"))
    match = lambda names: [n for n in names if n.lower() in text]
    matched, additional, learning_only, prohibited = match(core), match(extra), match(learning), match(not_claimed)
    senior = bool(re.search(r"\b(senior|lead|principal|staff|5\+\s*(years|лет))\b", text, re.I))
    office_only = "офис" in text and not any(x in text for x in ("удал", "remote"))
    unpaid_test = ("тестов" in text or "test task" in text) and any(x in text for x in ("неоплач", "unpaid"))
    required_years = _years_requirement(text)
    declared_years = profile.get("candidate", {}).get("experience_years")
    # Four direct core matches should clear the configured recommendation threshold;
    # learning-only terms remain a deliberately small signal.
    score = min(100, 30 + len(matched) * 11 + len(additional) * 4 + len(learning_only) - len(prohibited) * 22)
    risks: list[str] = []
    critical: list[str] = []
    if senior: score -= 35; critical.append("Senior-level requirement")
    if office_only: score -= 40; critical.append("Office-only work")
    if unpaid_test: score -= 30; critical.append("Large unpaid test task")
    if required_years and (not isinstance(declared_years, (int, float)) or declared_years < required_years):
        score -= 18
        risks.append(f"Requires {required_years}+ years; profile does not confirm this requirement")
    if prohibited: risks.append("Critical work relies on skills not claimed: " + ", ".join(prohibited))
    if learning_only: risks.append("Learning-only stack: " + ", ".join(learning_only))
    score = max(0, score)
    recommendation = "apply" if score >= thresholds.get("recommend_apply", 72) and not critical else "review" if score >= thresholds.get("manual_review", 60) else "skip"
    project = _portfolio_project(profile)
    years_unconfirmed = bool(required_years and (not isinstance(declared_years, (int, float)) or declared_years < required_years))
    return AnalysisResult(summary=job.summary or job.title, match_score=score, confidence=0.72, recommendation=recommendation, matched_skills=matched + additional, missing_skills=[], critical_requirements=critical, possible_risks=risks, reasoning="Deterministic profile match; LLM enhancement is optional.", response_strategy=[f"Mention {project}", "Describe only matched technologies", "Clarify scope and success criteria"], questions_for_client=["What is the first measurable outcome for this role?"], candidate_level_fit="good" if recommendation == "apply" else "review", required_skills_fit={"matched": matched, "partially_matched": additional, "learning_only": learning_only, "missing": [], "critical_missing": prohibited + critical}, best_portfolio_project=project, safe_claims_for_application=matched[:6], claims_to_avoid=prohibited + learning_only, auto_apply_allowed=score >= 88 and not critical and not prohibited and not years_unconfirmed)
