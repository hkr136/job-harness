from job_agent.database.repositories import Store
from job_agent.models import AnalysisResult, RawJobDetails
from job_agent.services.statistics_service import StatisticsService


def test_overview_is_computed_from_persisted_records(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    job, _ = store.upsert_job(RawJobDetails(external_job_id="1", site="sample", url="https://example.test/1", title="Role"))
    store.save_analysis(job.id, AnalysisResult(summary="", match_score=90, confidence=1, recommendation="apply", reasoning=""), model="local", tokens=100, cost_usd=0.01)
    store.save_draft(job.id, "sample", "draft")
    values = StatisticsService(store).overview()
    assert values["found"] == 1
    assert values["high_priority"] == 1
    assert values["llm_tokens"] == 100
    assert values["llm_cost_usd"] == 0.01
