from job_agent.analysis.matcher import analyze_locally
from job_agent.models import RawJobDetails


def test_undeclared_years_requirement_requires_review() -> None:
    profile = {"candidate": {"skill_levels": {"core": {}, "additional_experience": [], "learning": [], "not_claimed": []}, "experience": []}}
    job = RawJobDetails(external_job_id="1", site="example", url="https://example.test", title="Role", description="Опыт работы от 3 лет")
    result = analyze_locally(job, profile, {"manual_review": 60, "recommend_apply": 72})
    assert any("years" in risk for risk in result.possible_risks)
    assert not result.auto_apply_allowed
