from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
USER_HOME = Path(os.environ.get("JOB_HARNESS_HOME", Path.home() / ".job-harness")).expanduser()


def ensure_user_home() -> Path:
    for directory in (USER_HOME, USER_HOME / "artifacts", USER_HOME / "logs", USER_HOME / "browser-profiles"):
        directory.mkdir(parents=True, exist_ok=True)
    return USER_HOME


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=USER_HOME / ".env", env_prefix="JOB_AGENT_")

    database_url: str = f"sqlite:///{USER_HOME / 'state.sqlite3'}"
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_fallback_model: str | None = None
    llm_daily_budget_usd: float = 2.0
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1600
    llm_input_cost_per_million_usd: float = 0.0
    llm_output_cost_per_million_usd: float = 0.0
    # Scans must not create visible browser windows. Manual login is always headed.
    headless: bool = True
    browser_min_action_delay_seconds: float = 2.5
    browser_max_action_delay_seconds: float = 5.0


class LLMProviderSettings(BaseModel):
    """Connection metadata only. Personal keys remain in the user dotenv file."""
    type: str
    enabled: bool = True
    # ``codex_oauth`` means "use the owning Codex CLI session".  It never
    # exposes or copies that session token into Job Harness.
    auth: str = "api_key"
    model: str | None = None
    fallback_model: str | None = None
    base_url: str = ""
    api_key_env: str | None = None
    command: str = "codex"
    timeout_seconds: int = 180
    models: list["LLMModelSettings"] = Field(default_factory=list)


class LLMModelSettings(BaseModel):
    """A user-declared model for gateways that cannot enumerate a catalogue."""

    id: str
    label: str | None = None
    context_window: int | None = None
    stream: bool = True
    tool_calling: bool = True
    json_mode: bool = True
    enabled: bool = True


class LLMRoleSettings(BaseModel):
    enabled: bool = True
    provider: str | None = None
    model: str | None = None
    # Qualified ``provider/model`` references tried only before any streamed
    # token is emitted. This prevents silently stitching two model replies.
    fallbacks: list[str] = Field(default_factory=list)
    automated: bool = False


class LLMSettings(BaseModel):
    providers: dict[str, LLMProviderSettings] = Field(default_factory=dict)
    roles: dict[str, LLMRoleSettings] = Field(default_factory=dict)


class AppSettings(BaseModel):
    dry_run: bool = True
    timezone: str = "Europe/Moscow"


class LimitSettings(BaseModel):
    applications_per_day: int = 15
    messages_per_hour: int = 10
    llm_budget_per_day_usd: float = 2.0
    max_browser_retries: int = 3
    remote_task_timeout_seconds: int = 240


class ApplicationSettings(BaseModel):
    unattended_submission: bool = False
    auto_apply_threshold: int = 88
    auto_mode: bool = False
    auto_match_threshold: int = 60
    auto_review_threshold: int = 80
    auto_max_rewrite_attempts: int = 2
    auto_reply_messages: bool = True


class TuiSettings(BaseModel):
    """Visual preferences only; they never affect automation decisions."""

    motion: str = "heavy"
    vacancy_sort: str = "fresh"


class MatchingSettings(BaseModel):
    thresholds: dict[str, int] = Field(default_factory=lambda: {"show_job": 45, "manual_review": 60, "recommend_apply": 72, "high_priority": 85})
    skill_weights: dict[str, float] = Field(default_factory=dict)


class SiteSettings(BaseModel):
    enabled: bool = True
    adapter: str
    browser_profile: str
    login_url: str
    search: dict[str, Any] = Field(default_factory=dict)
    automation: dict[str, bool] = Field(default_factory=dict)


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    tui: TuiSettings = Field(default_factory=TuiSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    applications: ApplicationSettings = Field(default_factory=ApplicationSettings)
    matching: MatchingSettings = Field(default_factory=MatchingSettings)
    scheduler: dict[str, Any] = Field(default_factory=dict)
    sites: dict[str, SiteSettings] = Field(default_factory=dict)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    env: EnvSettings = Field(default_factory=EnvSettings)


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache
def get_settings() -> Settings:
    ensure_user_home()
    path = USER_HOME / "config.yaml"
    if not path.exists():
        path = ROOT / "config.example.yaml"
    data = _load_yaml(path)
    # One persisted source of truth. Existing user configs keep their prior
    # behaviour without forcing a manual migration.
    applications = data.setdefault("applications", {})
    applications.setdefault("auto_mode", bool(applications.get("unattended_submission", False)))
    data["env"] = EnvSettings().model_dump()
    return Settings.model_validate(data)


def load_profile() -> dict[str, Any]:
    path = USER_HOME / "profile.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Candidate profile is missing: {path}. Run `job-agent init` first.")
    return _load_yaml(path)


def save_tui_motion(motion: str) -> None:
    """Persist a visual-only preference in the user-owned configuration."""
    if motion not in {"light", "heavy"}:
        raise ValueError("motion must be light or heavy")
    ensure_user_home()
    path = USER_HOME / "config.yaml"
    data = _load_yaml(path) if path.exists() else _load_yaml(ROOT / "config.example.yaml")
    data.setdefault("tui", {})["motion"] = motion
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()


def save_tui_vacancy_sort(sort: str) -> None:
    if sort not in {"fresh", "analyzed", "score", "status", "site"}:
        raise ValueError("Unsupported vacancy sort")
    ensure_user_home()
    path = USER_HOME / "config.yaml"
    data = _load_yaml(path) if path.exists() else _load_yaml(ROOT / "config.example.yaml")
    data.setdefault("tui", {})["vacancy_sort"] = sort
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()


def save_llm_role(role: str, model_ref: str, *, fallbacks: list[str] | None = None) -> None:
    """Persist a role selection in the user-owned config, never source code."""
    if "/" not in model_ref:
        raise ValueError("model reference must be provider/model")
    provider, model = model_ref.split("/", 1)
    if not provider or not model:
        raise ValueError("model reference must be provider/model")
    ensure_user_home()
    path = USER_HOME / "config.yaml"
    data = _load_yaml(path) if path.exists() else _load_yaml(ROOT / "config.example.yaml")
    role_data = data.setdefault("llm", {}).setdefault("roles", {}).setdefault(role, {})
    role_data.update({"enabled": True, "provider": provider, "model": model})
    if fallbacks is not None:
        role_data["fallbacks"] = fallbacks
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()


def save_provider_enabled(provider_id: str, enabled: bool) -> None:
    """Toggle a provider in user configuration without touching credentials."""
    ensure_user_home()
    path = USER_HOME / "config.yaml"
    data = _load_yaml(path) if path.exists() else _load_yaml(ROOT / "config.example.yaml")
    providers = data.setdefault("llm", {}).setdefault("providers", {})
    if provider_id not in providers:
        raise ValueError(f"Unknown configured provider: {provider_id}")
    providers[provider_id]["enabled"] = enabled
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()


def save_site_enabled(site_id: str, enabled: bool) -> None:
    """Toggle a configured site from the Settings Hub."""
    ensure_user_home()
    path = USER_HOME / "config.yaml"
    data = _load_yaml(path) if path.exists() else _load_yaml(ROOT / "config.example.yaml")
    sites = data.setdefault("sites", {})
    if site_id not in sites:
        raise ValueError(f"Unknown configured site: {site_id}")
    sites[site_id]["enabled"] = enabled
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()


def save_scheduler_settings(
    *,
    enabled: bool | None = None,
    job_name: str | None = None,
    job_enabled: bool | None = None,
    interval_minutes: int | None = None,
) -> None:
    """Persist scheduler controls in the user-owned config.

    The scheduler process reads this same file on its next cycle, so the TUI
    never pretends to control a second, hidden state.
    """
    ensure_user_home()
    path = USER_HOME / "config.yaml"
    data = _load_yaml(path) if path.exists() else _load_yaml(ROOT / "config.example.yaml")
    scheduler = data.setdefault("scheduler", {})
    if enabled is not None:
        scheduler["enabled"] = enabled
    if job_name is not None:
        jobs = scheduler.setdefault("jobs", {})
        job = jobs.setdefault(job_name, {})
        if job_enabled is not None:
            job["enabled"] = job_enabled
        if interval_minutes is not None:
            job["interval_minutes"] = max(15, int(interval_minutes))
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()


def save_application_automation(
    *, unattended_submission: bool | None = None, auto_apply_threshold: int | None = None,
    auto_mode: bool | None = None, auto_match_threshold: int | None = None,
    auto_review_threshold: int | None = None, auto_max_rewrite_attempts: int | None = None,
    auto_reply_messages: bool | None = None,
) -> None:
    """Persist automatic-submission policy separately from manual ARMED sends."""
    ensure_user_home()
    path = USER_HOME / "config.yaml"
    data = _load_yaml(path) if path.exists() else _load_yaml(ROOT / "config.example.yaml")
    applications = data.setdefault("applications", {})
    if unattended_submission is not None:
        applications["unattended_submission"] = unattended_submission
    if auto_apply_threshold is not None:
        applications["auto_apply_threshold"] = min(100, max(0, int(auto_apply_threshold)))
    if auto_mode is not None:
        applications["auto_mode"] = auto_mode
        # Keep a legacy config's meaning aligned with the visible Auto mode.
        applications["unattended_submission"] = auto_mode
    for name, value, minimum, maximum in (
        ("auto_match_threshold", auto_match_threshold, 0, 100),
        ("auto_review_threshold", auto_review_threshold, 0, 100),
        ("auto_max_rewrite_attempts", auto_max_rewrite_attempts, 0, 2),
    ):
        if value is not None:
            applications[name] = min(maximum, max(minimum, int(value)))
    if auto_reply_messages is not None:
        applications["auto_reply_messages"] = auto_reply_messages
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()


def compensation_answer(profile: dict[str, Any]) -> str | None:
    """Format only a user-declared compensation expectation for form questions."""
    values = profile.get("candidate", {}).get("compensation", {})
    if not isinstance(values, dict):
        return None
    target = values.get("monthly_target") or values.get("monthly_min")
    minimum = values.get("monthly_min")
    currency = str(values.get("currency") or "").strip()
    if not isinstance(target, (int, float)) or not currency:
        return None
    amount = f"{int(target):,}".replace(",", " ")
    if isinstance(minimum, (int, float)) and minimum != target:
        minimum_text = f"{int(minimum):,}".replace(",", " ")
        return f"Минимум — {minimum_text} {currency} в месяц, комфортный ориентир — {amount} {currency} в месяц."
    return f"Ориентир по ожиданиям: от {amount} {currency} в месяц."
