import pytest

from job_agent.database.repositories import Store
from job_agent.models import AuthStatus, RawJob, RawJobDetails, SearchFilters
from job_agent.services.search_service import SearchService
from job_agent.sites.base import BaseSiteAdapter

PROFILE = {"candidate": {"skill_levels": {"core": {}, "additional_experience": [], "learning": [], "not_claimed": []}, "experience": []}}


class Adapter(BaseSiteAdapter):
    site_name = "sample"

    async def check_auth(self) -> AuthStatus:
        return AuthStatus(authenticated=True)

    async def search_jobs(self, filters: SearchFilters) -> list[RawJob]:
        return [RawJob(external_job_id="bad", site="sample", url="https://example.test/bad", title="Bad"), RawJob(external_job_id="ok", site="sample", url="https://example.test/ok", title="Ok")]

    async def get_job_details(self, external_job_id: str) -> RawJobDetails:
        if external_job_id == "bad":
            raise RuntimeError("gone")
        return RawJobDetails(external_job_id="ok", site="sample", url="https://example.test/ok", title="Ok", description="")


@pytest.mark.asyncio
async def test_scan_continues_after_failed_listing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("job_agent.services.search_service.load_profile", lambda: PROFILE)
    new, analyzed = await SearchService(Store(f"sqlite:///{tmp_path / 'state.sqlite3'}"), Adapter(), {}).scan(SearchFilters())
    assert (new, analyzed) == (1, 1)


@pytest.mark.asyncio
async def test_scan_does_not_reanalyze_unchanged_listing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("job_agent.services.search_service.load_profile", lambda: PROFILE)
    service = SearchService(Store(f"sqlite:///{tmp_path / 'state.sqlite3'}"), Adapter(), {})
    assert await service.scan(SearchFilters()) == (1, 1)
    assert await service.scan(SearchFilters()) == (0, 0)
