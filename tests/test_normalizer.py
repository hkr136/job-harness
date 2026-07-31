from job_agent.analysis.normalizer import normalize_locally
from job_agent.models import RawJobDetails


def test_local_normalizer_produces_a_uniform_readable_vacancy() -> None:
    raw = RawJobDetails(
        external_job_id="1", site="kwork", url="https://example.test", title="AI bot",
        description="Нужно доработать AI-бота. Подключить FastAPI и webhooks.", budget="до 25 000 ₽",
    )

    text = normalize_locally(raw).as_text()

    assert "ВАКАНСИЯ: AI bot" in text
    assert "ЗАДАЧА" in text
    assert "БЮДЖЕТ: до 25 000 ₽" in text
