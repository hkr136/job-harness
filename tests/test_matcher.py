from job_agent.analysis.matcher import analyze_locally
from job_agent.models import RawJobDetails

PROFILE = {
    "candidate": {
        "skill_levels": {
            "core": {"backend": {"technologies": ["Language Alpha", "Framework Alpha", "Protocol Alpha", "Service Alpha"]}},
            "additional_experience": ["Container Alpha"],
            "learning": ["Framework Beta"],
            "not_claimed": ["Platform Gamma"],
        },
        "experience": ["Synthetic integration project"],
    }
}


def test_core_stack_is_recommended() -> None:
    job = RawJobDetails(external_job_id="1", site="test", url="https://example.test/1", title="Language Alpha Framework Alpha", description="Remote Language Alpha Framework Alpha Protocol Alpha Service Alpha Container Alpha")
    result = analyze_locally(job, PROFILE, {"manual_review": 60, "recommend_apply": 72})
    assert result.recommendation == "apply"
    assert "Language Alpha" in result.matched_skills


def test_senior_frontend_is_not_auto_allowed() -> None:
    job = RawJobDetails(external_job_id="2", site="test", url="https://example.test/2", title="Senior Framework Beta engineer", description="Senior Framework Beta Platform Gamma commercial experience")
    result = analyze_locally(job, PROFILE, {"manual_review": 60, "recommend_apply": 72})
    assert result.recommendation == "skip"
    assert not result.auto_apply_allowed


def test_matcher_accepts_grouped_experience_in_user_profile() -> None:
    profile = {**PROFILE, "candidate": {**PROFILE["candidate"], "experience": {"projects": [{"name": "An example project"}]}}}
    job = RawJobDetails(external_job_id="3", site="test", url="https://example.test/3", title="Language Alpha role", description="Language Alpha")
    result = analyze_locally(job, profile, {"manual_review": 60, "recommend_apply": 72})
    assert result.best_portfolio_project == "An example project"
