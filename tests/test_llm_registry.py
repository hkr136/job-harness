import pytest

from job_agent.config.settings import Settings
from job_agent.llm.registry import discover_registry


@pytest.mark.asyncio
async def test_registry_reports_disabled_provider_without_network() -> None:
    settings = Settings.model_validate({
        "llm": {"providers": {"gateway": {
            "type": "openai_compatible", "enabled": False, "auth": "none",
            "base_url": "http://127.0.0.1:9999/v1",
            "models": [{"id": "local-model", "stream": True}],
        }}},
    })
    registry = await discover_registry(settings)

    assert registry[0].availability == "disabled"
    assert registry[0].models[0].qualified_id == "gateway/local-model"


@pytest.mark.asyncio
async def test_registry_never_probes_missing_api_key() -> None:
    settings = Settings.model_validate({
        "llm": {"providers": {"remote": {
            "type": "openai_compatible", "enabled": True, "auth": "api_key",
            "api_key_env": "TEST_MISSING_KEY", "base_url": "https://example.invalid/v1",
        }}},
    })
    registry = await discover_registry(settings)

    assert registry[0].availability == "needs_api_key"
