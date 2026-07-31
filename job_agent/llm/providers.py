from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

import httpx
from dotenv import dotenv_values

from job_agent.config.settings import USER_HOME


@dataclass(frozen=True)
class ProviderStatus:
    ready: bool
    detail: str


class LLMProvider(Protocol):
    provider_id: str
    model: str | None
    last_tokens: int
    last_cost_usd: float

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...
    async def stream_complete(self, system: str, user: str, *, json_mode: bool = False) -> AsyncIterator[str]: ...
    async def list_models(self) -> list[str]: ...
    async def check_connection(self) -> ProviderStatus: ...


def resolve_secret(name: str | None) -> str | None:
    """Read a named secret from the process or the user-owned dotenv file."""
    if not name:
        return None
    return os.environ.get(name) or dotenv_values(USER_HOME / ".env").get(name)


class OpenAICompatibleProvider:
    """Provider for OpenAI-compatible chat-completion APIs, including OpenRouter."""

    def __init__(
        self,
        provider_id: str,
        api_key: str | None,
        base_url: str,
        model: str | None,
        fallback_model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1600,
        input_cost_per_million_usd: float = 0.0,
        output_cost_per_million_usd: float = 0.0,
        auth_required: bool = True,
    ) -> None:
        self.provider_id, self.api_key = provider_id, api_key
        self.base_url, self.model, self.fallback_model = base_url.rstrip("/"), model, fallback_model
        self.temperature, self.max_tokens = temperature, max_tokens
        self.input_cost_per_million_usd = input_cost_per_million_usd
        self.output_cost_per_million_usd = output_cost_per_million_usd
        self.last_tokens = 0
        self.last_cost_usd = 0.0
        self.auth_required = auth_required

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if self.auth_required and not self.api_key:
            raise RuntimeError(f"{self.provider_id}: API key is not configured")
        if not self.model:
            raise RuntimeError(f"{self.provider_id}: choose a model first")
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=payload)
                response.raise_for_status()
            except (httpx.HTTPError, KeyError):
                if not self.fallback_model or self.fallback_model == self.model:
                    raise
                payload["model"] = self.fallback_model
                response = await client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=payload)
                response.raise_for_status()
        body = response.json()
        usage = body.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        self.last_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
        self.last_cost_usd = (prompt_tokens * self.input_cost_per_million_usd + completion_tokens * self.output_cost_per_million_usd) / 1_000_000
        return str(body["choices"][0]["message"]["content"])

    async def stream_complete(self, system: str, user: str, *, json_mode: bool = False) -> AsyncIterator[str]:
        """Yield actual provider deltas from the OpenAI-compatible SSE stream."""
        if self.auth_required and not self.api_key:
            raise RuntimeError(f"{self.provider_id}: API key is not configured")
        if not self.model:
            raise RuntimeError(f"{self.provider_id}: choose a model first")
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, read=90)) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = line.removeprefix("data:").strip()
                    if event == "[DONE]":
                        break
                    try:
                        content = json.loads(event)["choices"][0].get("delta", {}).get("content", "")
                    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(content, str) and content:
                        yield content

    async def list_models(self) -> list[str]:
        if self.auth_required and not self.api_key:
            return []
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base_url}/models", headers=self._headers())
            response.raise_for_status()
        return sorted(str(item["id"]) for item in response.json().get("data", []) if item.get("id"))

    async def check_connection(self) -> ProviderStatus:
        if self.auth_required and not self.api_key:
            return ProviderStatus(False, "API key is not configured")
        try:
            models = await self.list_models()
            return ProviderStatus(True, f"connected; {len(models)} model(s) available")
        except Exception as error:
            return ProviderStatus(False, f"connection failed: {type(error).__name__}: {error}")


class OllamaProvider(OpenAICompatibleProvider):
    """Ollama's local OpenAI-compatible chat API with its native model catalogue."""

    def __init__(self, provider_id: str, base_url: str, model: str | None, **kwargs: Any) -> None:
        super().__init__(provider_id, None, base_url.rstrip("/") + "/v1", model, auth_required=False, **kwargs)
        self.ollama_url = base_url.rstrip("/")

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self.ollama_url}/api/tags")
            response.raise_for_status()
        return sorted(str(item["name"]) for item in response.json().get("models", []) if item.get("name"))


class CodexCLIProvider:
    """Runs the user's already authenticated Codex CLI; no ChatGPT token is read or copied."""

    def __init__(self, provider_id: str, command: str = "codex", model: str | None = None, timeout_seconds: int = 180) -> None:
        self.provider_id, self.command, self.model = provider_id, command, model
        self.timeout_seconds = timeout_seconds
        self.last_tokens = 0
        self.last_cost_usd = 0.0

    async def check_connection(self) -> ProviderStatus:
        def check() -> ProviderStatus:
            try:
                result = subprocess.run([self.command, "--version"], capture_output=True, text=True, timeout=15, check=False)
            except FileNotFoundError:
                return ProviderStatus(False, f"{self.command} was not found in PATH")
            except subprocess.TimeoutExpired:
                return ProviderStatus(False, f"{self.command} did not respond in time")
            if result.returncode != 0:
                return ProviderStatus(False, (result.stderr or "Codex CLI returned an error").strip()[:240])
            # This asks the CLI only for its login state.  The OAuth token
            # remains wholly inside Codex's own credential store.
            login = subprocess.run([self.command, "login", "status"], capture_output=True, text=True, timeout=15, check=False)
            if login.returncode != 0 or "logged in" not in (login.stdout + login.stderr).lower():
                return ProviderStatus(False, (login.stderr or login.stdout or "Codex login is required").strip()[:240])
            version = (result.stdout or result.stderr or "installed").strip().splitlines()[0]
            return ProviderStatus(True, f"Codex CLI authenticated ({version})")
        return await asyncio.to_thread(check)

    async def list_models(self) -> list[str]:
        # Codex CLI manages its own account-supported model catalogue. A blank model means its default.
        return [self.model] if self.model else []

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        status = await self.check_connection()
        if not status.ready:
            raise RuntimeError(status.detail)
        suffix = " Return strictly valid JSON and no Markdown." if json_mode else " Return only the requested text."
        prompt = f"{system}\n\n{user}{suffix}\nDo not modify files, run commands, access the network, or take external actions."

        def run() -> str:
            with tempfile.TemporaryDirectory(prefix="job-agent-codex-") as directory:
                output = Path(directory) / "response.txt"
                command = [self.command, "exec", "--skip-git-repo-check", "--output-last-message", str(output)]
                if self.model:
                    command.extend(["--model", self.model])
                command.append(prompt)
                result = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or result.stdout or "Codex CLI failed").strip()[:1000])
                if not output.exists():
                    raise RuntimeError("Codex CLI returned no final response")
                return output.read_text(encoding="utf-8").strip()
        return await asyncio.to_thread(run)

    async def stream_complete(self, system: str, user: str, *, json_mode: bool = False) -> AsyncIterator[str]:
        """Stream actual ``item/agentMessage/delta`` events from Codex app-server.

        ``codex exec --json`` intentionally reports completed items only. The
        local app-server protocol exposes the same authenticated account with
        incremental agent-message deltas, so no API key or fake typewriter is
        needed for the TUI.
        """
        status = await self.check_connection()
        if not status.ready:
            raise RuntimeError(status.detail)
        suffix = " Return strictly valid JSON and no Markdown." if json_mode else " Return only the requested text."
        prompt = f"{system}\n\n{user}{suffix}\nDo not modify files, run commands, access the network, or take external actions."
        yielded = False
        final_message = ""
        started_turn = False

        async with _codex_app_server(self.command, self.model, self.timeout_seconds, prompt) as stream:
            async for event in stream:
                if event[0] == "delta":
                    yielded = True
                    yield event[1]
                elif event[0] == "final":
                    final_message = event[1]
                elif event[0] == "turn_started":
                    started_turn = True
        if not yielded and final_message:
            yield final_message
        elif not yielded and not started_turn:
            # Compatibility fallback for old Codex installations which do not
            # provide the app-server protocol. It is deliberately not used
            # after a turn begins, preventing a duplicate model request.
            yield await self.complete(system, user, json_mode=json_mode)


class _codex_app_server:
    """Minimal JSON-RPC client for Codex app-server token deltas."""

    def __init__(self, command: str, model: str | None, timeout: int, prompt: str) -> None:
        self.command, self.model, self.timeout, self.prompt = command, model, timeout, prompt
        self.process: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> AsyncIterator[tuple[str, str]]:
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self.events()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def send(self, request_id: int, method: str, params: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode())
        await self.process.stdin.drain()

    async def next_message(self) -> dict[str, Any]:
        assert self.process is not None and self.process.stdout is not None
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=self.timeout)
        except TimeoutError as error:
            raise RuntimeError("Codex app-server did not produce a streaming event in time") from error
        if not line:
            raise RuntimeError("Codex app-server closed before returning a response")
        try:
            return json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("Codex app-server returned invalid JSON-RPC") from error

    async def wait_for_response(self, request_id: int) -> dict[str, Any]:
        while True:
            message = await self.next_message()
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result", {})

    async def events(self) -> AsyncIterator[tuple[str, str]]:
        await self.send(1, "initialize", {"clientInfo": {"name": "job-harness", "version": "0.1"}})
        await self.wait_for_response(1)
        with tempfile.TemporaryDirectory(prefix="job-agent-codex-stream-") as directory:
            await self.send(
                2,
                "thread/start",
                {
                    "cwd": directory,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "model": self.model,
                    "baseInstructions": "Do not modify files, run commands, access the network, or take external actions.",
                },
            )
            thread = (await self.wait_for_response(2)).get("thread", {})
            thread_id = thread.get("id")
            if not isinstance(thread_id, str):
                raise RuntimeError("Codex app-server did not return a thread ID")
            await self.send(3, "turn/start", {"threadId": thread_id, "approvalPolicy": "never", "input": [{"type": "text", "text": self.prompt}]})
            await self.wait_for_response(3)
            while True:
                message = await self.next_message()
                method = message.get("method")
                params = message.get("params", {})
                if method == "item/agentMessage/delta" and isinstance(params.get("delta"), str):
                    yield "delta", params["delta"]
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                        yield "final", item["text"]
                elif method == "turn/completed":
                    return


def create_provider(provider_id: str, config: Any, *, default_temperature: float = 0.2, default_max_tokens: int = 1600) -> LLMProvider:
    """Instantiate a provider from generic user-owned configuration."""
    kind = config.type
    if kind == "codex_cli":
        return CodexCLIProvider(provider_id, config.command, config.model, config.timeout_seconds)
    kwargs = {"temperature": default_temperature, "max_tokens": default_max_tokens}
    if kind == "ollama":
        return OllamaProvider(provider_id, config.base_url, config.model, **kwargs)
    if kind in {"openai_compatible", "openai", "openrouter"}:
        return OpenAICompatibleProvider(
            provider_id,
            resolve_secret(config.api_key_env),
            config.base_url,
            config.model,
            config.fallback_model,
            auth_required=getattr(config, "auth", "api_key") != "none",
            **kwargs,
        )
    for plugin in entry_points(group="job_agent.providers"):
        if plugin.name == kind:
            factory = plugin.load()
            return factory(provider_id, config, default_temperature=default_temperature, default_max_tokens=default_max_tokens)
    raise ValueError(f"Unknown LLM provider type: {kind}")
