import pytest

from job_agent.services.recovery_service import propose_adapter_recovery


class Adapter:
    async def search_jobs(self):
        return []


class Provider:
    provider_id = "test"
    model = "test"
    last_tokens = 0
    last_cost_usd = 0.0

    async def complete(self, system, user, *, json_mode=False):
        assert "Current adapter source" in user
        return "```diff\n+ safer selector\n```\n\nVerify with site test."

    async def list_models(self):
        return []

    async def check_connection(self):
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_recovery_writes_reviewable_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("job_agent.services.recovery_service.USER_HOME", tmp_path)
    path = await propose_adapter_recovery(Provider(), Adapter(), "sample", "selector timeout")
    assert path.exists()
    assert "Recovery proposal: sample" in path.read_text(encoding="utf-8")
