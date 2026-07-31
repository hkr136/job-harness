from job_agent.models import AnalysisResult
from job_agent.services.application_service import ApplicationService


class Adapter:
    class capabilities:
        submit_application = False


def test_application_policy_rejects_unsupported_site(tmp_path) -> None:
    from job_agent.database.repositories import Store

    analysis = AnalysisResult(summary="", match_score=100, confidence=1, recommendation="apply", reasoning="")
    allowed, reason = ApplicationService(Store(f"sqlite:///{tmp_path / 'state.sqlite3'}"), Adapter(), 15, 88).eligible(analysis)
    assert not allowed
    assert "does not support" in reason
