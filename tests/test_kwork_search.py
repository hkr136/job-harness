from job_agent.sites.kwork.adapter import KworkAdapter


def test_kwork_project_feed_parses_real_project_urls_not_seller_buttons() -> None:
    jobs = KworkAdapter.parse_project_links(
        [
            {"href": "/projects/3227043", "text": "Доработка готового AI-бота на Python"},
            {"href": "/projects/list/customer", "text": "Смотреть открытые"},
            {"href": "/new_offer?project=3227043", "text": "Предложить услугу"},
            {"href": "/projects/3227056/view", "text": "Внедрение AI агентов"},
        ],
        max_results=40,
    )

    assert [job.external_job_id for job in jobs] == ["3227043", "3227056"]
    assert jobs[0].url == "https://kwork.ru/projects/3227043/view"


def test_kwork_detail_strips_navigation_and_footer() -> None:
    body = "ФРИЛАНС МАРКЕТПЛЕЙС\nК списку проектов\nAI бот\nНужно доработать FastAPI и webhooks\nО Kwork\nПомощь"

    detail = KworkAdapter.extract_project_description(body, "AI бот")

    assert detail == "Нужно доработать FastAPI и webhooks"
