import asyncio

from job_agent.database.repositories import Store
from job_agent.models import ExternalApplicationStatus, RawJobDetails
from job_agent.services.status_service import StatusService


class Adapter:
    site_name = "sample"

    class capabilities:
        application_statuses = True

    async def get_application_statuses(self):
        return [ExternalApplicationStatus(external_application_id="ext-1", status="viewed", detail="Read")]


def test_status_import_only_creates_known_local_vacancies(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    store.upsert_job(RawJobDetails(external_job_id="ext-1", site="sample", url="https://example.test/1", title="Role"))
    assert asyncio.run(StatusService(store, Adapter()).import_known()) == (1, 0)
    application = store.list_applications()[0]
    assert application.external_application_id == "ext-1"
    assert application.status == "viewed"


def test_record_confirmed_external_application_requires_existing_job(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    store.upsert_job(RawJobDetails(external_job_id="ext-2", site="sample", url="https://example.test/2", title="Role"))
    application = store.record_confirmed_external_application(
        "sample", "ext-2", "Personalized response", "Success banner observed."
    )
    assert application.status == "submitted"
    assert application.final_text == "Personalized response"
    assert application.external_application_id == "ext-2"
