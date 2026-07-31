from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from job_agent.models import (
    AuthStatus,
    ExternalApplicationStatus,
    PreparedApplication,
    RawJob,
    RawJobDetails,
    RawMessage,
    SearchFilters,
    SendMessageResult,
    SubmissionResult,
)


@dataclass(frozen=True)
class SiteCapabilities:
    search_jobs: bool = True
    submit_application: bool = False
    read_messages: bool = False
    send_messages: bool = False
    application_statuses: bool = False
    saved_searches: bool = False


class BaseSiteAdapter(ABC):
    site_name: str
    capabilities = SiteCapabilities()

    @abstractmethod
    async def check_auth(self) -> AuthStatus: ...
    @abstractmethod
    async def search_jobs(self, filters: SearchFilters) -> list[RawJob]: ...
    @abstractmethod
    async def get_job_details(self, external_job_id: str) -> RawJobDetails: ...

    async def collect_job_details(self, filters: SearchFilters) -> list[RawJobDetails]:
        """Collect a batch. Adapters may override this to reuse one browser context."""
        details: list[RawJobDetails] = []
        for job in await self.search_jobs(filters):
            try:
                details.append(await self.get_job_details(job.external_job_id))
            except Exception:
                # A deleted card should not make a whole site's batch unusable.
                continue
        return details

    async def prepare_application(self, job_id: int, response: str) -> PreparedApplication:
        return PreparedApplication(job_id=job_id, site=self.site_name, body=response)

    async def submit_application(self, prepared_application: PreparedApplication, confirm: bool = False) -> SubmissionResult:
        return SubmissionResult(success=False, confirmed=False, detail="This adapter does not support submission yet.")

    async def get_application_statuses(self) -> list[ExternalApplicationStatus]:
        return []

    async def get_unread_messages(self) -> list[RawMessage]:
        return []

    async def send_message(self, conversation_id: str, text: str, confirm: bool = False) -> SendMessageResult:
        return SendMessageResult(success=False, confirmed=False, detail="This adapter does not support messages yet.")
