import json

import pytest

from job_agent.analysis.llm import analyze_with_provider
from job_agent.config.settings import Settings
from job_agent.llm.factory import provider_for_role, role_is_automated
from job_agent.llm.providers import CodexCLIProvider, OpenAICompatibleProvider
from job_agent.models import RawJobDetails


class FakeProvider:
    provider_id = "fake"
    model = "test-model"
    last_tokens = 12
    last_cost_usd = 0.0

    async def complete(self, system, user, *, json_mode=False):
        assert json_mode
        return json.dumps({"summary": "ok", "match_score": 70, "confidence": 0.8, "recommendation": "review", "reasoning": "evidence"})

    async def list_models(self):
        return [self.model]

    async def check_connection(self):
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_analysis_uses_generic_provider_contract() -> None:
    job = RawJobDetails(external_job_id="1", site="sample", url="https://example.test", title="Role")
    result = await analyze_with_provider(FakeProvider(), job, {"candidate": {}})
    assert result.match_score == 70


def test_role_selection_is_provider_neutral() -> None:
    settings = Settings.model_validate({
        "llm": {
            "providers": {"local-codex": {"type": "codex_cli", "command": "codex", "model": None}},
            "roles": {"analysis": {"provider": "local-codex", "model": "chosen-model", "automated": True}},
        }
    })
    provider = provider_for_role(settings, "analysis")
    assert isinstance(provider, CodexCLIProvider)
    assert provider.model == "chosen-model"
    assert role_is_automated(settings, "analysis")


@pytest.mark.asyncio
async def test_openai_compatible_provider_requires_key_before_request() -> None:
    provider = OpenAICompatibleProvider("remote", None, "https://example.test/v1", "model")
    with pytest.raises(RuntimeError, match="API key"):
        await provider.complete("system", "user")
