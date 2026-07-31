import sys
from types import ModuleType

from job_agent.models import AuthStatus, RawJob, RawJobDetails, SearchFilters
from job_agent.sites.base import BaseSiteAdapter
from job_agent.sites.registry import build_adapter


class CustomAdapter(BaseSiteAdapter):
    site_name = "custom"

    def __init__(self, browser, profile: str) -> None:
        self.browser, self.profile = browser, profile

    async def check_auth(self) -> AuthStatus:
        return AuthStatus(authenticated=True)

    async def search_jobs(self, filters: SearchFilters) -> list[RawJob]:
        return []

    async def get_job_details(self, external_job_id: str) -> RawJobDetails:
        raise NotImplementedError


def test_registry_loads_custom_adapter_from_user_selected_module(monkeypatch) -> None:
    module = ModuleType("job_agent_test_custom_adapter")
    module.CustomAdapter = CustomAdapter
    monkeypatch.setitem(sys.modules, module.__name__, module)

    adapter = build_adapter("job_agent_test_custom_adapter:CustomAdapter", browser="browser", profile="profile")

    assert isinstance(adapter, CustomAdapter)
    assert adapter.profile == "profile"
