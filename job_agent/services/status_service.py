from __future__ import annotations

from job_agent.database.repositories import Store
from job_agent.sites.base import BaseSiteAdapter


class StatusService:
    """Sync only externally identified applications from an adapter's confirmed state."""

    def __init__(self, store: Store, adapter: BaseSiteAdapter) -> None:
        self.store, self.adapter = store, adapter

    async def sync(self) -> int:
        if not self.adapter.capabilities.application_statuses:
            return 0
        changed = 0
        for item in await self.adapter.get_application_statuses():
            changed += self.store.set_application_status_by_external(
                self.adapter.site_name,
                item.external_application_id,
                item.status,
                item.detail,
            )
        return changed

    async def import_known(self) -> tuple[int, int]:
        """Import response-list rows only where the vacancy is already known locally."""
        if not self.adapter.capabilities.application_statuses:
            return 0, 0
        imported, skipped = 0, 0
        for item in await self.adapter.get_application_statuses():
            if self.store.import_external_application(self.adapter.site_name, item.external_application_id, item.status, item.detail):
                imported += 1
            else:
                skipped += 1
        return imported, skipped
