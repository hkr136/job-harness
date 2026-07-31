"""User-facing provider and model discovery.

The registry describes availability; it never reads OAuth credentials or
places secrets in logs.  It is intentionally small so a user can add a
standard OpenAI-compatible gateway without changing application code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from job_agent.config.settings import LLMProviderSettings, Settings
from job_agent.llm.providers import create_provider, resolve_secret

ModelAvailability = Literal[
    "available", "needs_login", "needs_api_key", "local_runtime_offline", "unreachable", "disabled"
]

@dataclass(frozen=True)
class ModelDescriptor:
    qualified_id: str
    provider_id: str
    model_id: str
    source: str
    stream: bool = True
    tool_calling: bool = True
    json_mode: bool = True
    context_window: int | None = None


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    kind: str
    auth: str
    availability: ModelAvailability
    detail: str
    discovery: bool
    capabilities: tuple[str, ...] = ("stream", "tools", "json")
    models: tuple[ModelDescriptor, ...] = ()


def _configured_models(provider_id: str, config: LLMProviderSettings) -> list[ModelDescriptor]:
    models: list[ModelDescriptor] = []
    if config.model:
        models.append(ModelDescriptor(f"{provider_id}/{config.model}", provider_id, config.model, "selected"))
    for item in config.models:
        if item.enabled and all(existing.model_id != item.id for existing in models):
            models.append(
                ModelDescriptor(
                    f"{provider_id}/{item.id}", provider_id, item.id, "configured", item.stream,
                    item.tool_calling, item.json_mode, item.context_window,
                )
            )
    return models


async def discover_provider(settings: Settings, provider_id: str, config: LLMProviderSettings) -> ProviderDescriptor:
    """Probe a provider without generating a completion or changing state."""
    # Older user configs predate the explicit auth field. Their provider type
    # is still enough to infer the safe, non-secret connection method.
    auth = "codex_oauth" if config.type == "codex_cli" else "none" if config.type == "ollama" else config.auth
    if auth != config.auth:
        config = config.model_copy(update={"auth": auth})
    configured = _configured_models(provider_id, config)
    if not config.enabled:
        return ProviderDescriptor(provider_id, config.type, auth, "disabled", "Disabled in config.yaml", False, models=tuple(configured))
    if auth == "api_key" and not resolve_secret(config.api_key_env):
        return ProviderDescriptor(provider_id, config.type, auth, "needs_api_key", f"Set {config.api_key_env or 'an API-key env name'} in ~/.job-harness/.env", True, models=tuple(configured))
    provider = create_provider(provider_id, config, default_temperature=settings.env.llm_temperature, default_max_tokens=settings.env.llm_max_tokens)
    status = await provider.check_connection()
    if not status.ready:
        availability: ModelAvailability
        if auth == "codex_oauth":
            availability = "needs_login"
        elif config.type == "ollama":
            availability = "local_runtime_offline"
        else:
            availability = "unreachable"
        return ProviderDescriptor(provider_id, config.type, auth, availability, status.detail, True, models=tuple(configured))
    try:
        discovered = await provider.list_models()
    except Exception as error:
        discovered = []
        detail = f"{status.detail}; catalogue unavailable: {type(error).__name__}"
    else:
        detail = status.detail
    for model in discovered:
        if all(item.model_id != model for item in configured):
            configured.append(ModelDescriptor(f"{provider_id}/{model}", provider_id, model, "discovered"))
    if config.type == "codex_cli" and not configured:
        configured.append(ModelDescriptor(f"{provider_id}/default", provider_id, "default", "account default"))
    return ProviderDescriptor(provider_id, config.type, auth, "available", detail, True, models=tuple(configured))


async def discover_registry(settings: Settings) -> list[ProviderDescriptor]:
    """Discover providers concurrently; a broken endpoint cannot hide peers."""
    async def one(provider_id: str, config: LLMProviderSettings) -> ProviderDescriptor:
        try:
            return await discover_provider(settings, provider_id, config)
        except Exception as error:
            return ProviderDescriptor(provider_id, config.type, config.auth, "unreachable", f"Discovery failed: {type(error).__name__}: {error}", True)

    return list(await asyncio.gather(*(one(provider_id, config) for provider_id, config in settings.llm.providers.items())))
