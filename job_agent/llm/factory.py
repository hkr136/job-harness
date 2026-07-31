from __future__ import annotations

from collections.abc import AsyncIterator

from job_agent.config.settings import Settings
from job_agent.llm.providers import LLMProvider, create_provider


class FailoverProvider:
    """Role-level failover which is safe for streamed replies.

    A secondary provider is considered only before the first delta. Once a
    user sees text, preserving one coherent answer matters more than hiding a
    transient connection failure.
    """

    def __init__(self, providers: list[LLMProvider]) -> None:
        self.providers = providers
        self.provider_id = providers[0].provider_id
        self.model = providers[0].model
        self.last_tokens = 0
        self.last_cost_usd = 0.0
        self.last_used: str | None = None
        self.fallback_reason: str | None = None

    def _record(self, provider: LLMProvider, reason: str | None = None) -> None:
        self.provider_id, self.model = provider.provider_id, provider.model
        self.last_tokens, self.last_cost_usd = provider.last_tokens, provider.last_cost_usd
        self.last_used = f"{provider.provider_id}/{provider.model or 'default'}"
        self.fallback_reason = reason

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        errors: list[str] = []
        for provider in self.providers:
            try:
                result = await provider.complete(system, user, json_mode=json_mode)
                self._record(provider, "; ".join(errors) or None)
                return result
            except Exception as error:
                errors.append(f"{provider.provider_id}: {type(error).__name__}")
        raise RuntimeError("; ".join(errors) or "No configured provider")

    async def stream_complete(self, system: str, user: str, *, json_mode: bool = False) -> AsyncIterator[str]:
        errors: list[str] = []
        for provider in self.providers:
            started = False
            try:
                async for delta in provider.stream_complete(system, user, json_mode=json_mode):
                    started = True
                    yield delta
                self._record(provider, "; ".join(errors) or None)
                return
            except Exception as error:
                if started:
                    self._record(provider, "; ".join(errors) or None)
                    raise
                errors.append(f"{provider.provider_id}: {type(error).__name__}")
        raise RuntimeError("; ".join(errors) or "No configured provider")

    async def list_models(self) -> list[str]:
        return await self.providers[0].list_models()

    async def check_connection(self):  # type: ignore[no-untyped-def]
        return await self.providers[0].check_connection()


def _provider(settings: Settings, provider_id: str, model: str | None) -> LLMProvider | None:
    config = settings.llm.providers.get(provider_id)
    if not config or not config.enabled:
        return None
    if model:
        config = config.model_copy(update={"model": model})
    return create_provider(provider_id, config, default_temperature=settings.env.llm_temperature, default_max_tokens=settings.env.llm_max_tokens)


def provider_for_role(settings: Settings, role: str) -> LLMProvider | None:
    """Resolve one role through the universal provider registry.

    Legacy single-provider environment variables remain supported until a user
    selects a provider in their own config.yaml.
    """
    choice = settings.llm.roles.get(role)
    if choice and choice.enabled and choice.provider:
        primary = _provider(settings, choice.provider, choice.model)
        if primary is None:
            return None
        providers = [primary]
        for reference in choice.fallbacks:
            provider_id, separator, model = reference.partition("/")
            if not separator:
                continue
            fallback = _provider(settings, provider_id, model)
            if fallback is not None:
                providers.append(fallback)
        return primary if len(providers) == 1 else FailoverProvider(providers)
    return None


def role_is_automated(settings: Settings, role: str) -> bool:
    choice = settings.llm.roles.get(role)
    return bool(choice and choice.enabled and choice.automated)
