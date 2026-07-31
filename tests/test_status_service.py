import asyncio

from job_agent.models import ExternalApplicationStatus
from job_agent.services.status_service import StatusService


class Adapter:
    site_name = "example"

    class capabilities:
        application_statuses = True

    async def get_application_statuses(self):
        return [ExternalApplicationStatus(external_application_id="ext-1", status="viewed", detail="Seen")]


def test_status_service_ignores_unmapped_external_application(tmp_path) -> None:
    from job_agent.database.repositories import Store

    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    assert asyncio.run(StatusService(store, Adapter()).sync()) == 0
