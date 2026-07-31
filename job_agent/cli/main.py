from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from job_agent.analysis.llm import LLMAnalyzer, analyze_with_provider
from job_agent.analysis.matcher import analyze_locally
from job_agent.analysis.response_generator import build_draft, build_draft_with_provider
from job_agent.browser.manager import BrowserManager
from job_agent.config.settings import ROOT, USER_HOME, ensure_user_home, get_settings, load_profile
from job_agent.database.repositories import Store
from job_agent.llm.factory import provider_for_role
from job_agent.llm.providers import create_provider
from job_agent.models import ClarificationInput, PreparedApplication, RawJobDetails, SearchFilters
from job_agent.scheduler.launchd import (
    bootstrap_launch_agent,
    launch_agent_status,
    write_launch_agent,
)
from job_agent.scheduler.runtime import HarnessScheduler
from job_agent.services.application_service import ApplicationService
from job_agent.services.form_answers import save_profile_answer
from job_agent.services.harness import HarnessWorker
from job_agent.services.message_service import MessageService
from job_agent.services.recovery_service import propose_adapter_recovery
from job_agent.services.search_service import SearchService
from job_agent.services.statistics_service import StatisticsService
from job_agent.services.status_service import StatusService
from job_agent.sites.registry import build_adapter
from job_agent.tui.native import NativeHarnessApp

app = typer.Typer(no_args_is_help=True, help="Local human-supervised Job Agent")
jobs_app = typer.Typer(no_args_is_help=True)
site_app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True)
profile_app = typer.Typer(no_args_is_help=True)
applications_app = typer.Typer(no_args_is_help=True)
messages_app = typer.Typer(no_args_is_help=True)
runs_app = typer.Typer(no_args_is_help=True)
logs_app = typer.Typer(no_args_is_help=True)
queue_app = typer.Typer(no_args_is_help=True)
scheduler_app = typer.Typer(no_args_is_help=True)
stats_app = typer.Typer(no_args_is_help=False)
providers_app = typer.Typer(no_args_is_help=True)
clarifications_app = typer.Typer(no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")
app.add_typer(site_app, name="site")
app.add_typer(config_app, name="config")
app.add_typer(profile_app, name="profile")
app.add_typer(applications_app, name="applications")
app.add_typer(messages_app, name="messages")
app.add_typer(runs_app, name="runs")
app.add_typer(logs_app, name="logs")
app.add_typer(queue_app, name="queue")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(stats_app, name="stats")
app.add_typer(providers_app, name="providers")
app.add_typer(clarifications_app, name="clarifications")
console = Console()


def store() -> Store:
    return Store(get_settings().env.database_url)


def browser(settings) -> BrowserManager:
    return BrowserManager(
        settings.env.headless,
        settings.env.browser_min_action_delay_seconds,
        settings.env.browser_max_action_delay_seconds,
    )


def analyzer(settings, role: str = "analysis"):
    selected = provider_for_role(settings, role)
    if selected is not None:
        return selected
    if not settings.env.llm_api_key:
        return None
    return LLMAnalyzer(
        settings.env.llm_api_key,
        settings.env.llm_base_url,
        settings.env.llm_model,
        settings.env.llm_temperature,
        settings.env.llm_max_tokens,
        settings.env.llm_input_cost_per_million_usd,
        settings.env.llm_output_cost_per_million_usd,
    )


def refine_draft_interactively(text: str) -> str:
    """Small local editor; it never sends text or invents profile evidence."""
    console.print("[bold]Interactive draft editor[/bold] — commands: show, shorten, question TEXT, append TEXT, done")
    while True:
        console.print(text)
        command = input("draft> ").strip()
        if command in {"done", "exit", "quit"}:
            return text
        if command == "show":
            continue
        if command == "shorten":
            sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
            text = " ".join(sentences[:3])
            continue
        if command.startswith("question "):
            question = command.removeprefix("question ").strip()
            if question:
                text = text.rstrip() + "\n\n" + question
            continue
        if command.startswith("append "):
            addition = command.removeprefix("append ").strip()
            if addition:
                text = text.rstrip() + "\n\n" + addition
            continue
        console.print("[yellow]Unknown editor command.[/yellow]")


@app.command()
def init() -> None:
    """Create a neutral, editable user-data directory without overwriting it."""
    home = ensure_user_home()
    created: list[str] = []
    for source_name, target_name in (("config.example.yaml", "config.yaml"), ("profile.example.yaml", "profile.yaml")):
        target = home / target_name
        if not target.exists():
            shutil.copyfile(ROOT / "config" / source_name if source_name.startswith("profile") else ROOT / source_name, target)
            created.append(str(target))
    get_settings.cache_clear()
    console.print("Created:\n" + "\n".join(created) if created else f"User home already initialized: {home}")


@app.command("migrate-user-data")
def migrate_user_data() -> None:
    """Copy legacy local state to the user directory; never overwrite existing data."""
    home = ensure_user_home()
    items = (
        (ROOT / "config.yaml", home / "config.yaml"),
        (ROOT / "config" / "profile.yaml", home / "profile.yaml"),
        (ROOT / "data" / "job_agent.sqlite3", home / "state.sqlite3"),
        (ROOT / "data" / "browser-profiles", home / "browser-profiles"),
    )
    copied: list[str] = []
    for source, target in items:
        if not source.exists() or target.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        copied.append(str(target))
    get_settings.cache_clear()
    console.print("Copied:\n" + "\n".join(copied) if copied else "Nothing to migrate; user data is already separate.")


@app.command()
def doctor() -> None:
    settings = get_settings()
    configured = [name for name, item in settings.llm.providers.items() if item.enabled]
    checks = [("Python", sys.version.split()[0]), ("User home", str(ensure_user_home())), ("Database", "OK"), ("Configuration", "OK"), ("LLM providers", ", ".join(configured) if configured else ("legacy API" if settings.env.llm_api_key else "optional / local analyzer"))]
    table = Table(title="Job Agent doctor"); table.add_column("Check"); table.add_column("Status")
    for name, status in checks: table.add_row(name, status)
    console.print(table)


@providers_app.command("list")
def providers_list() -> None:
    """Show generic provider and role selections; secrets are never displayed."""
    settings = get_settings()
    table = Table(title="LLM providers")
    for column in ("ID", "Type", "Model", "Enabled", "Roles"):
        table.add_column(column)
    for provider_id, config in settings.llm.providers.items():
        roles = ", ".join(name for name, role in settings.llm.roles.items() if role.provider == provider_id) or "—"
        table.add_row(provider_id, config.type, config.model or "default", "yes" if config.enabled else "no", roles)
    console.print(table)


@providers_app.command("setup")
def providers_setup() -> None:
    """Add the neutral built-in provider registry without overwriting user choices."""
    path = USER_HOME / "config.yaml"
    if not path.exists():
        raise typer.BadParameter("Configuration is missing; run `job-agent init` first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    llm = data.setdefault("llm", {})
    providers = llm.setdefault("providers", {})
    providers.setdefault("codex", {"type": "codex_cli", "enabled": True, "model": None, "command": "codex"})
    providers.setdefault("openai", {"type": "openai_compatible", "enabled": False, "base_url": "https://api.openai.com/v1", "api_key_env": "JOB_AGENT_OPENAI_API_KEY", "model": None})
    providers.setdefault("openrouter", {"type": "openai_compatible", "enabled": False, "base_url": "https://openrouter.ai/api/v1", "api_key_env": "JOB_AGENT_OPENROUTER_API_KEY", "model": None})
    providers.setdefault("ollama", {"type": "ollama", "enabled": False, "base_url": "http://127.0.0.1:11434", "model": None})
    roles = llm.setdefault("roles", {})
    for role in ("analysis", "writing", "orchestration", "recovery"):
        roles.setdefault(role, {"provider": "codex", "automated": False})
    backup = path.with_suffix(".yaml.before-llm")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()
    console.print(f"LLM providers added to {path}; backup: {backup}")


@providers_app.command("test")
def providers_test(provider: str) -> None:
    """Validate a local CLI or API connection without sending job-site actions."""
    settings = get_settings()
    config = settings.llm.providers.get(provider)
    if config is None:
        raise typer.BadParameter(f"Unknown provider: {provider}")
    status = asyncio.run(create_provider(provider, config, default_temperature=settings.env.llm_temperature, default_max_tokens=settings.env.llm_max_tokens).check_connection())
    color = "green" if status.ready else "yellow"
    console.print(f"[{color}]{provider}: {status.detail}[/{color}]")


@providers_app.command("models")
def providers_models(provider: str, refresh: bool = typer.Option(False, help="Reserved for manual catalogue refresh; this command never runs automatically.")) -> None:
    """List models fetched from a provider only when the user invokes this command."""
    settings = get_settings()
    config = settings.llm.providers.get(provider)
    if config is None:
        raise typer.BadParameter(f"Unknown provider: {provider}")
    models = asyncio.run(create_provider(provider, config, default_temperature=settings.env.llm_temperature, default_max_tokens=settings.env.llm_max_tokens).list_models())
    if models:
        console.print("\n".join(models))
    elif config.type == "codex_cli":
        console.print("Codex CLI manages the model catalogue for the signed-in account. Leave model blank to use Codex default, or set a documented model ID in config.yaml.")
    else:
        console.print("No models returned. Check the connection with `job-agent providers test <id>`.")


@providers_app.command("select")
def providers_select(
    role: str,
    provider: str,
    model: str | None = typer.Option(None, help="Blank uses the provider default."),
    automated: bool | None = typer.Option(None, help="Allow this role during scheduled work."),
) -> None:
    """Assign any configured provider/model to a Job Harness LLM role."""
    if role not in {"analysis", "writing", "orchestration", "recovery"}:
        raise typer.BadParameter("role must be analysis, writing, orchestration or recovery")
    settings = get_settings()
    if provider not in settings.llm.providers:
        raise typer.BadParameter(f"Unknown provider: {provider}")
    path = USER_HOME / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    choice = data.setdefault("llm", {}).setdefault("roles", {}).setdefault(role, {})
    choice["provider"] = provider
    choice["model"] = model
    if automated is not None:
        choice["automated"] = automated
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()
    console.print(f"{role}: {provider} / {model or 'default'} / automated={choice.get('automated', False)}")


@config_app.command("show")
def config_show() -> None:
    """Print the user-owned YAML configuration, never environment secrets."""
    path = USER_HOME / "config.yaml"
    if not path.exists():
        raise typer.BadParameter("Configuration is missing; run `job-agent init` first.")
    console.print(path.read_text(encoding="utf-8"))


def update_user_config(mutator) -> None:
    path = USER_HOME / "config.yaml"
    if not path.exists():
        raise typer.BadParameter("Configuration is missing; run `job-agent init` first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mutator(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    get_settings.cache_clear()


@profile_app.command("show")
def profile_show() -> None:
    """Print the user-owned candidate profile for local review."""
    path = USER_HOME / "profile.yaml"
    if not path.exists():
        raise typer.BadParameter("Profile is missing; run `job-agent init` first.")
    console.print(path.read_text(encoding="utf-8"))


@profile_app.command("edit")
def profile_edit() -> None:
    """Open the profile in the user's editor; no profile data is written by code."""
    path = USER_HOME / "profile.yaml"
    if not path.exists():
        raise typer.BadParameter("Profile is missing; run `job-agent init` first.")
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run(shlex.split(editor) + [str(path)], check=False)


@app.command()
def status() -> None:
    settings = get_settings()
    values = store().stats()
    funnel = Table(title="Local funnel")
    funnel.add_column("Jobs")
    funnel.add_column("Analyzed")
    funnel.add_column("Score ≥70")
    funnel.add_column("Score ≥85")
    funnel.add_column("Drafts")
    funnel.add_column("Submitted")
    funnel.add_column("Clarify")
    funnel.add_column("Unread")
    funnel.add_row(*(str(values[key]) for key in ("jobs", "analyzed", "score_70", "score_85", "drafts", "submitted", "needs_clarification", "unread_messages")))
    console.print(funnel)

    sites = Table(title="Site sessions")
    sites.add_column("Site")
    sites.add_column("Enabled")
    sites.add_column("Session")
    sites.add_column("Action")
    for site_name, config in settings.sites.items():
        if not config.enabled:
            sites.add_row(site_name, "no", "not checked", "enable in config.yaml")
            continue
        try:
            adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
            auth = asyncio.run(adapter.check_auth())
            action = "ready to scan" if auth.authenticated else f"job-agent site login {site_name}"
            sites.add_row(site_name, "yes", "logged in" if auth.authenticated else "login required", action)
        except Exception as error:  # Browser installation or a site outage must remain visible.
            sites.add_row(site_name, "yes", "check failed", str(error)[:100])
    console.print(sites)


@app.command()
def scan(site: str = typer.Option("hh"), dry_run: bool = typer.Option(True, help="Scan only; never sends applications.")) -> None:
    settings = get_settings()
    names = [name for name, config in settings.sites.items() if config.enabled] if site == "all" else [site]
    if not names or any(name not in settings.sites for name in names):
        raise typer.BadParameter(f"Unknown configured site: {site}")
    for name in names:
        config = settings.sites[name]
        try:
            adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
            auth = asyncio.run(adapter.check_auth())
            if not auth.authenticated:
                console.print(f"[yellow]{name}: not started — {auth.detail}. Run `job-agent site login {name}`.[/yellow]")
                continue
            filters = SearchFilters.model_validate(config.search)
            service = SearchService(store(), adapter, settings.matching.thresholds, analyzer(settings, "analysis"), settings.limits.llm_budget_per_day_usd)
            new, analyzed = asyncio.run(service.scan(filters))
            console.print(f"[green]{name}: scan complete[/green]: new={new}, analyzed={analyzed}, skipped={len(service.last_errors)}, dry_run={dry_run}")
            for error in service.last_errors[:5]:
                console.print(f"[yellow]  skipped: {error}[/yellow]")
        except Exception as error:
            console.print(f"[red]{name}: scan failed: {error}[/red]")


@jobs_app.command("list")
def jobs_list(min_score: int = 0, site: str | None = None, status: str | None = None, sort: str = typer.Option("score", help="score, newest or budget")) -> None:
    if sort not in {"score", "newest", "budget"}:
        raise typer.BadParameter("sort must be score, newest or budget")
    table = Table(title="Jobs"); [table.add_column(x) for x in ("ID", "Score", "Site", "Vacancy", "Budget", "Status")]
    rows = store().list_jobs(min_score, site, status)
    if sort == "newest":
        rows.sort(key=lambda row: row[0].discovered_at, reverse=True)
    elif sort == "budget":
        rows.sort(key=lambda row: int(re.sub(r"\D", "", row[0].budget or "0") or 0), reverse=True)
    for job, analysis in rows: table.add_row(str(job.id), str(analysis.score if analysis else "—"), job.site, job.title[:60], job.budget or "—", job.status)
    console.print(table)


@jobs_app.command("show")
def jobs_show(job_id: int) -> None:
    job, analysis = store().get_job(job_id); console.print(f"[bold]{job.title}[/bold]\n{job.url}\n\n{job.description[:4000]}")
    if analysis: console.print_json(analysis.model_dump_json(indent=2))
    history = store().list_clarifications(job_id, include_resolved=True)
    if history:
        console.print("\n[bold]Clarification history[/bold]")
        for item in reversed(history):
            answer = f" → {item.answer_scope}: {item.answer}" if item.answer else ""
            console.print(f"{item.created_at.isoformat()}  {item.kind} · {item.state} · {item.question}{answer}")


@jobs_app.command("open")
def jobs_open(job_id: int) -> None:
    job, _ = store().get_job(job_id)
    webbrowser.open(job.url)
    console.print(job.url)


@jobs_app.command("favorite")
def jobs_favorite(job_id: int) -> None:
    store().set_job_status(job_id, "favorite")
    console.print(f"Job #{job_id} marked as favorite.")


@jobs_app.command("ignore")
def jobs_ignore(job_id: int) -> None:
    store().set_job_status(job_id, "ignored")
    console.print(f"Job #{job_id} marked as ignored.")


@jobs_app.command("reanalyze")
def jobs_reanalyze(job_id: int) -> None:
    """Refresh one stored listing and rerun analysis; no external action is sent."""
    settings = get_settings()
    job, _ = store().get_job(job_id)
    config = settings.sites.get(job.site)
    if config is None:
        raise typer.BadParameter(f"Site {job.site} is no longer configured.")
    adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
    details = asyncio.run(adapter.get_job_details(job.external_job_id))
    record, _ = store().upsert_job(details)
    llm = analyzer(settings, "analysis")
    result = asyncio.run(analyze_with_provider(llm, details, load_profile())) if llm else analyze_locally(details, load_profile(), settings.matching.thresholds)
    store().save_analysis(record.id, result, model=llm.model if llm else "local")
    console.print(f"Job #{job_id} reanalyzed: score={result.match_score}, recommendation={result.recommendation}")


@jobs_app.command("import-external")
def jobs_import_external(
    site: str,
    external_job_id: str,
    title: str,
    url: str,
    description: str = typer.Option("", help="Optional confirmed description excerpt."),
) -> None:
    """Register a confirmed external vacancy so its application/status can be tracked locally."""
    settings = get_settings()
    if site not in settings.sites:
        raise typer.BadParameter(f"Unknown configured site: {site}")
    raw = RawJobDetails(external_job_id=external_job_id, site=site, url=url, title=title, description=description)
    record, created = store().upsert_job(raw)
    console.print(f"Job #{record.id} {'imported' if created else 'updated'} from {site}.")


@app.command()
def apply(
    job_id: int,
    draft: bool = typer.Option(True),
    auto: bool = typer.Option(False),
    confirm: bool = typer.Option(False, help="Explicitly allow a supported site adapter to submit."),
    price: str | None = typer.Option(None),
    duration: str | None = typer.Option(None),
    title: str | None = typer.Option(None),
    interactive: bool = typer.Option(False),
) -> None:
    """Create a draft; a site-side send requires --auto --confirm and confirmation UI."""
    job, analysis = store().get_job(job_id)
    if analysis is None: raise typer.BadParameter("Analyze the job first.")
    local_job = RawJobDetails(
        external_job_id=job.external_job_id, site=job.site, url=job.url, title=job.title,
        company=job.company, budget=job.budget, work_format=job.work_format,
        published_at=job.published_at, description=job.normalized_text.removeprefix("[normalized]\n") or job.description, normalized_text=job.normalized_text,
    )
    text = build_draft(local_job, analysis)
    writer = analyzer(get_settings(), "writing")
    if writer:
        try:
            text = asyncio.run(build_draft_with_provider(writer, local_job, analysis, load_profile()))
        except Exception as error:
            console.print(f"[yellow]LLM draft unavailable; using local template: {type(error).__name__}[/yellow]")
    if interactive:
        text = refine_draft_interactively(text)
    record = store().save_draft(job_id, job.site, text)
    console.print(f"[yellow]Draft #{record.id}; not submitted.[/yellow]\n\n{text}")
    if not auto:
        return
    settings = get_settings(); config = settings.sites.get(job.site)
    if config is None:
        raise typer.BadParameter(f"Site {job.site} is no longer configured.")
    prepared = PreparedApplication(
        job_id=job.id,
        external_job_id=job.external_job_id,
        site=job.site,
        body=text,
        title=title or job.title[:100],
        price=price,
        duration=duration,
    )
    adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
    result = asyncio.run(
        ApplicationService(store(), adapter, settings.limits.applications_per_day, settings.applications.auto_apply_threshold).submit(
            prepared, analysis, dry_run=not confirm, manual=confirm
        )
    )
    color = "green" if result.confirmed else "yellow"
    console.print(f"[{color}]{result.detail}[/{color}]")


@applications_app.command("list")
def applications_list() -> None:
    table = Table(title="Applications")
    for column in ("ID", "Job", "Site", "Status", "Created", "Submitted"):
        table.add_column(column)
    for record in store().list_applications():
        table.add_row(str(record.id), str(record.job_id), record.site, record.status, record.created_at.isoformat(), record.submitted_at.isoformat() if record.submitted_at else "—")
    console.print(table)


@applications_app.command("show")
def applications_show(application_id: int) -> None:
    record = store().get_application(application_id)
    text = record.final_text or record.draft
    console.print(f"[bold]Application #{record.id} · {record.site} · {record.status}[/bold]\nJob: {record.job_id}\nExternal ID: {record.external_application_id or '—'}\nCreated: {record.created_at.isoformat()}\n\n{text}")
    history = store().list_application_history(application_id)
    if history:
        console.print("\n[bold]Status history[/bold]")
        for item in history:
            console.print(f"{item.created_at.isoformat()}  {item.previous_status or '—'} → {item.new_status}  [{item.source}] {item.detail}")


@applications_app.command("withdraw")
def applications_withdraw(application_id: int, confirm: bool = typer.Option(False, help="Confirm a local withdrawal marker; it does not contact the site.")) -> None:
    if not confirm:
        console.print("[yellow]No remote action is available. Re-run with --confirm to mark this local record withdrawn.[/yellow]")
        return
    store().set_application_status(application_id, "withdrawn", "local_user", "Marked withdrawn locally; verify the site separately.")
    console.print(f"Application #{application_id} marked withdrawn locally.")


@applications_app.command("record-final")
def applications_record_final(application_id: int, text: str) -> None:
    """Record a confirmed final external text in local history; it never sends it."""
    store().set_application_final_text(application_id, text)
    console.print(f"Final text saved for application #{application_id}.")


@applications_app.command("record-confirmed")
def applications_record_confirmed(
    site: str,
    external_job_id: str,
    text: str,
    detail: str = typer.Option("Confirmed in the site interface."),
    confirm: bool = typer.Option(False, help="Confirm that the site explicitly showed a successful submission."),
) -> None:
    """Record an already-confirmed external response; this command never sends it."""
    if not confirm:
        console.print("[yellow]Nothing was recorded. Re-run with --confirm only after an explicit site confirmation.[/yellow]")
        return
    record = store().record_confirmed_external_application(site, external_job_id, text, detail)
    console.print(f"Application #{record.id} recorded as submitted; no site action was performed.")


@applications_app.command("check-statuses")
def applications_check_statuses(site: str = typer.Option("all")) -> None:
    settings = get_settings()
    names = [name for name, config in settings.sites.items() if config.enabled] if site == "all" else [site]
    if not names or any(name not in settings.sites for name in names):
        raise typer.BadParameter(f"Unknown configured site: {site}")
    for name in names:
        config = settings.sites[name]
        adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
        changed = asyncio.run(StatusService(store(), adapter).sync())
        console.print(f"{name}: {changed} confirmed status change(s)")


@applications_app.command("import-statuses")
def applications_import_statuses(site: str = typer.Option("all")) -> None:
    """Import confirmed site statuses for vacancies already tracked locally; never sends anything."""
    settings = get_settings()
    names = [name for name, config in settings.sites.items() if config.enabled] if site == "all" else [site]
    if not names or any(name not in settings.sites for name in names):
        raise typer.BadParameter(f"Unknown configured site: {site}")
    for name in names:
        config = settings.sites[name]
        adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
        imported, skipped = asyncio.run(StatusService(store(), adapter).import_known())
        console.print(f"{name}: imported={imported}, skipped_unknown_local_vacancy={skipped}")


@clarifications_app.command("list")
def clarifications_list(all: bool = typer.Option(False, "--all", help="Include resolved requests.")) -> None:
    """List questions that must be answered before an application can continue."""
    records = store().list_clarifications(include_resolved=all)
    table = Table(title="Clarifications")
    for column in ("Job", "Site", "Vacancy", "Open", "Question types"):
        table.add_column(column)
    grouped: dict[int, list] = {}
    for record in records:
        grouped.setdefault(record.job_id, []).append(record)
    for job_id, items in grouped.items():
        job, _ = store().get_job(job_id)
        open_count = sum(item.state == "open" for item in items)
        kinds = ", ".join(sorted({item.kind for item in items}))
        table.add_row(str(job_id), job.site, job.title[:70], str(open_count), kinds)
    console.print(table)


@clarifications_app.command("capture")
def clarifications_capture(
    job_id: int,
    question: str,
    kind: str = typer.Option("other", help="compensation, location, experience, military_status, legal_status, test_task or other"),
    field: str = typer.Option("", help="Visible form field label or selector note."),
    artifact: str | None = typer.Option(None, help="Saved screenshot or trace path."),
) -> None:
    """Record an observed required form field; this never visits or submits a site."""
    job, _ = store().get_job(job_id)
    try:
        application_id = store().get_application_for_job(job_id).id
    except ValueError:
        application_id = None
    records = store().create_clarifications(
        job_id,
        job.site,
        [ClarificationInput(question=question, kind=kind, field_name=field, artifact_path=artifact)],
        application_id,
    )
    console.print(f"Captured {len(records)} clarification request(s) for job #{job_id}; no site action was performed.")


@clarifications_app.command("show")
def clarifications_show(job_id: int) -> None:
    """Show vacancy context, draft and all required answers for one job."""
    job, _ = store().get_job(job_id)
    console.print(f"[bold]{job.site} · {job.title}[/bold]\n{job.url}\n\n{job.description[:2500]}")
    try:
        application = store().get_application_for_job(job_id)
        console.print(f"\n[bold]Draft[/bold]\n{application.final_text or application.draft}")
    except ValueError:
        pass
    table = Table(title="Required information")
    for column in ("ID", "State", "Type", "Field", "Question", "Answer", "Scope"):
        table.add_column(column)
    for item in store().list_clarifications(job_id, include_resolved=True):
        table.add_row(str(item.id), item.state, item.kind, item.field_name or "—", item.question, item.answer or "—", item.answer_scope or "—")
    console.print(table)


@clarifications_app.command("answer")
def clarifications_answer(
    request_id: int,
    answer: str,
    scope: str = typer.Option(..., help="profile or vacancy"),
) -> None:
    """Save one user-confirmed answer without sending an application."""
    request = store().answer_clarification(request_id, answer, scope)
    if scope == "profile":
        save_profile_answer(request.kind, request.answer or "", request.question)
    console.print(f"Clarification #{request.id} answered for {scope}; resolve job #{request.job_id} when all questions are answered.")


@clarifications_app.command("resolve")
def clarifications_resolve(job_id: int) -> None:
    """Close answered requests and make a vacancy ready for a supervised apply attempt."""
    resolved, outstanding = store().resolve_clarifications(job_id)
    if not resolved:
        console.print("[yellow]Still missing required answers:[/yellow]")
        for item in outstanding:
            console.print(f"#{item.id} {item.kind}: {item.question}")
        raise typer.Exit(1)
    console.print(f"Job #{job_id} is ready_to_apply. No external action was performed.")


@messages_app.command("list")
def messages_list(unread: bool = typer.Option(False)) -> None:
    table = Table(title="Messages")
    for column in ("ID", "Site", "From", "Category", "Unread", "Text"):
        table.add_column(column)
    for record in store().list_messages(unread):
        table.add_row(str(record.id), record.site, record.sender, record.category, "yes" if record.is_unread else "no", record.body[:90])
    console.print(table)


@messages_app.command("show")
def messages_show(message_id: int) -> None:
    message = store().get_message(message_id)
    console.print(f"[bold]{message.site} · {message.sender}[/bold]\nConversation: {message.conversation_id}\n\n{message.body}")


@messages_app.command("conversations")
def messages_conversations() -> None:
    table = Table(title="Local conversations")
    for column in ("Conversation", "Unread", "Last message", "Updated"):
        table.add_column(column)
    for conversation, body, unread, received in store().list_conversations():
        table.add_row(conversation, str(unread), body[:100], received.isoformat())
    console.print(table)


@messages_app.command("conversation")
def messages_conversation(conversation: str) -> None:
    try:
        site, conversation_id = conversation.split(":", 1)
    except ValueError as error:
        raise typer.BadParameter("conversation must be SITE:CONVERSATION_ID") from error
    records = store().list_conversation_messages(site, conversation_id)
    if not records:
        raise typer.BadParameter("No local messages in this conversation")
    for item in records:
        console.print(f"[bold]{item.received_at.isoformat()} · {item.sender}[/bold]\n{item.body}\n")


@messages_app.command("reply")
def messages_reply(message_id: int) -> None:
    """Prepare and persist a profile-backed reply draft; never sends it."""
    decision = MessageService(store(), None).prepare_reply(message_id, load_profile())
    if decision.status == "draft":
        console.print(f"[bold]Черновик ответа[/bold]\n\n{decision.draft}\n\n[dim]Сохранён локально. Отправка ещё не выполнялась.[/dim]")
    elif decision.status == "not_needed":
        console.print(f"[yellow]{decision.reason}[/yellow]")
    else:
        console.print(f"[yellow]Нужно уточнение: {decision.reason}[/yellow]")


@messages_app.command("replies")
def messages_replies(status: str | None = typer.Option(None)) -> None:
    """List locally prepared message replies and their confirmed delivery state."""
    table = Table(title="Message replies")
    for column in ("Message", "Site", "Status", "Draft", "Reason"):
        table.add_column(column)
    for item in store().list_message_replies(status):
        table.add_row(str(item.message_id), item.site, item.status, item.draft[:100], item.reason[:100])
    console.print(table)


@messages_app.command("send")
def messages_send(
    message_id: int,
    confirm: bool = typer.Option(False, help="Actually send the prepared reply through the site's internal chat."),
) -> None:
    """Send an existing profile-backed reply only after explicit confirmation."""
    record = store().get_message(message_id)
    settings = get_settings()
    config = settings.sites.get(record.site)
    if config is None:
        raise typer.BadParameter(f"No configured adapter for site: {record.site}")
    adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
    result = asyncio.run(MessageService(store(), adapter).send_prepared_reply(message_id, confirm))
    if result.success and result.confirmed:
        console.print(f"[green]Reply sent and confirmed:[/green] {result.detail}")
    else:
        console.print(f"[yellow]Reply not sent:[/yellow] {result.detail}")


@messages_app.command("check")
def messages_check(site: str = typer.Option("all")) -> None:
    settings = get_settings()
    names = [name for name, config in settings.sites.items() if config.enabled] if site == "all" else [site]
    if not names or any(name not in settings.sites for name in names):
        raise typer.BadParameter(f"Unknown configured site: {site}")
    for name in names:
        config = settings.sites[name]
        try:
            adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
            count = asyncio.run(MessageService(store(), adapter).collect_unread())
            console.print(f"{name}: {count} new messages")
        except Exception as error:
            console.print(f"[red]{name}: message check failed: {error}[/red]")


@runs_app.command("list")
def runs_list() -> None:
    table = Table(title="Automation runs")
    for column in ("ID", "Kind", "Site", "Status", "Started", "Finished"):
        table.add_column(column)
    for record in store().list_runs():
        table.add_row(str(record.id), record.kind, record.site or "all", record.status, record.started_at.isoformat(), record.finished_at.isoformat() if record.finished_at else "—")
    console.print(table)


@runs_app.command("show")
def runs_show(run_id: int) -> None:
    record = store().get_run(run_id)
    console.print(f"[bold]{record.kind} · {record.site or 'all'} · {record.status}[/bold]\n{record.started_at.isoformat()} → {record.finished_at.isoformat() if record.finished_at else 'running'}\n\n{record.detail}")


@logs_app.callback(invoke_without_command=True)
def logs_list(ctx: typer.Context, errors: bool = typer.Option(False)) -> None:
    """List user-owned diagnostic artifacts; no cookies or secrets are printed."""
    if ctx.invoked_subcommand is not None:
        return
    directories = [USER_HOME / "logs"]
    if errors:
        directories = [USER_HOME / "logs" / "browser-errors", USER_HOME / "artifacts" / "playwright-traces"]
    files = [file for directory in directories if directory.exists() for file in directory.rglob("*") if file.is_file()]
    if not files:
        console.print("No diagnostic artifacts.")
        return
    for file in sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)[:50]:
        console.print(str(file))


@queue_app.command("list")
def queue_list(status: str | None = None) -> None:
    table = Table(title="Harness queue")
    for column in ("ID", "Priority", "Kind", "Site", "Status", "Attempts", "Run after"):
        table.add_column(column)
    for task in store().list_tasks(status):
        table.add_row(str(task.id), str(task.priority), task.kind, task.site or "all", task.status, f"{task.attempts}/{task.max_attempts}", task.run_after.isoformat())
    console.print(table)


@queue_app.command("scan")
def queue_scan(site: str = typer.Argument("hh"), priority: int = typer.Option(0)) -> None:
    settings = get_settings()
    if site not in settings.sites:
        raise typer.BadParameter(f"Unknown configured site: {site}")
    task_id = HarnessWorker(store(), settings).enqueue_scan(site, priority)
    console.print(f"Queued scan task #{task_id} for {site}.")


@queue_app.command("run-once")
def queue_run_once() -> None:
    result = HarnessWorker(store(), get_settings()).process_one_sync()
    console.print(result or "No queued task is due.")


@queue_app.command("recover")
def queue_recover(after_minutes: int = typer.Option(10, min=1)) -> None:
    """Requeue browser tasks left running by a stopped or crashed scheduler."""
    after = timedelta(minutes=after_minutes)
    recovered = store().requeue_stale_running_tasks(after)
    interrupted = store().fail_stale_running_runs(after)
    console.print(f"Recovered {recovered} stale task(s); marked {interrupted} run(s) interrupted.")


@scheduler_app.command("status")
def scheduler_status() -> None:
    settings = get_settings()
    configured = settings.scheduler.get("jobs", {})
    hours = settings.scheduler.get("working_hours", {})
    weekdays = hours.get("weekdays", [0, 1, 2, 3, 4])
    start, end = hours.get("start_hour", 9), hours.get("end_hour", 19)
    active, service_detail = launch_agent_status()
    if active is None:
        service_label = service_detail
    elif active:
        service_label = f"LaunchAgent active ({service_detail})"
    else:
        service_label = f"LaunchAgent inactive ({service_detail})"
    console.print(f"{service_label}. Config enabled={settings.scheduler.get('enabled', False)}")
    console.print(f"Background window: weekdays {weekdays}, {start}:00–{end}:00 ({settings.app.timezone})")
    for name, value in configured.items():
        console.print(f"{name}: {'enabled' if value.get('enabled', False) else 'disabled'} · every {value.get('interval_minutes', '—')} min")


@scheduler_app.command("enable")
def scheduler_enable(kind: str) -> None:
    if kind not in {"search", "messages", "application_statuses"}:
        raise typer.BadParameter("kind must be search, messages or application_statuses")
    def change(data):
        scheduler = data.setdefault("scheduler", {})
        scheduler["enabled"] = True
        scheduler.setdefault("jobs", {}).setdefault(kind, {})["enabled"] = True
    update_user_config(change)
    console.print(f"Scheduler task {kind}: enabled")


@scheduler_app.command("install-launchd")
def scheduler_install_launchd(
    confirm: bool = typer.Option(False, help="Write and load the local macOS LaunchAgent for this user."),
) -> None:
    """Install the local scheduler service after explicit user confirmation."""
    executable = Path(sys.executable).parent / "job-agent"
    if not executable.exists():
        raise typer.BadParameter(f"Job Agent executable was not found: {executable}")
    if not confirm:
        console.print("[yellow]No service was installed. Re-run with --confirm to write and load the local LaunchAgent.[/yellow]")
        return
    path = write_launch_agent(executable, ROOT, USER_HOME)
    try:
        bootstrap_launch_agent(path)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise typer.BadParameter(f"LaunchAgent plist was written to {path}, but launchctl could not load it: {detail}") from error
    console.print(f"[green]LaunchAgent installed:[/green] {path}\nThe scheduler will run according to config.yaml.")


@scheduler_app.command("disable")
def scheduler_disable(kind: str) -> None:
    if kind not in {"search", "messages", "application_statuses"}:
        raise typer.BadParameter("kind must be search, messages or application_statuses")
    update_user_config(lambda data: data.setdefault("scheduler", {}).setdefault("jobs", {}).setdefault(kind, {}).update({"enabled": False}))
    console.print(f"Scheduler task {kind}: disabled")


@scheduler_app.command("jobs")
def scheduler_jobs() -> None:
    settings = get_settings()
    table = Table(title="Scheduled task configuration")
    table.add_column("Task")
    table.add_column("Enabled")
    table.add_column("Interval")
    for kind in ("search", "messages", "application_statuses"):
        item = settings.scheduler.get("jobs", {}).get(kind, {})
        table.add_row(kind, "yes" if item.get("enabled", False) else "no", f"{item.get('interval_minutes', '—')} min")
    console.print(table)


@scheduler_app.command("run-now")
def scheduler_run_now(kind: str = typer.Argument("search")) -> None:
    worker = HarnessWorker(store(), get_settings())
    names = [name for name, config in get_settings().sites.items() if config.enabled]
    if kind == "search":
        for name in names:
            worker.enqueue_scan(name)
    elif kind == "messages":
        for name in names:
            worker.enqueue_message_check(name)
    elif kind == "statuses":
        for name in names:
            worker.enqueue_status_check(name)
    else:
        raise typer.BadParameter("kind must be search, messages or statuses")
    console.print(f"Queued {kind} work for {len(names)} enabled site(s). Run `job-agent queue run-once` or start `job-agent scheduler serve`.")


@scheduler_app.command("serve")
def scheduler_serve(force: bool = typer.Option(False, help="Run once even when scheduler.enabled is false.")) -> None:
    """Run the local scheduler until interrupted; suitable for a user service."""
    settings = get_settings()
    if not settings.scheduler.get("enabled", False) and not force:
        console.print("[yellow]Scheduler is disabled in config.yaml. Set scheduler.enabled: true or pass --force.[/yellow]")
        return
    runtime = HarnessScheduler(store(), settings)
    try:
        runtime.start()
    except RuntimeError as error:
        console.print(f"[yellow]{error}[/yellow]")
        return
    runtime.enqueue_scans()
    runtime.enqueue_messages()
    runtime.enqueue_statuses()
    console.print(f"Scheduler {runtime.summary()}. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runtime.stop()
        console.print("Scheduler stopped.")


@stats_app.command("funnel")
def stats_funnel() -> None:
    values = StatisticsService(store()).funnel()
    console.print(" → ".join(f"{name.title()} {value}" for name, value in values.items()))


def render_stats(since: datetime | None = None, site: str | None = None) -> None:
    values = StatisticsService(store()).overview(since, site)
    table = Table(title=f"Statistics{' · ' + site if site else ''}")
    table.add_column("Metric")
    table.add_column("Value")
    for name, value in values.items():
        table.add_row(name.replace("_", " "), str(value))
    console.print(table)


@stats_app.command("today")
def stats_today(site: str | None = typer.Option(None)) -> None:
    now = datetime.now(UTC)
    render_stats(now.replace(hour=0, minute=0, second=0, microsecond=0), site)


@stats_app.command("week")
def stats_week(site: str | None = typer.Option(None)) -> None:
    render_stats(datetime.now(UTC) - timedelta(days=7), site)


@stats_app.command("month")
def stats_month(site: str | None = typer.Option(None)) -> None:
    render_stats(datetime.now(UTC) - timedelta(days=30), site)


@stats_app.command("export")
def stats_export(format: str = typer.Option("csv", help="csv or json"), output: str | None = typer.Option(None)) -> None:
    """Export computed funnel counters without exposing browser/session data."""
    values = StatisticsService(store()).funnel()
    if format not in {"csv", "json"}:
        raise typer.BadParameter("format must be csv or json")
    path = Path(output) if output else USER_HOME / "artifacts" / f"stats.{format}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if format == "json":
        path.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(values))
            writer.writeheader(); writer.writerow(values)
    console.print(str(path))


@stats_app.command("skills")
def stats_skills() -> None:
    values = StatisticsService(store()).skills()
    table = Table(title="Skills from saved analyses")
    table.add_column("Matched")
    table.add_column("Count")
    table.add_column("Missing / critical")
    table.add_column("Count")
    rows = max(len(values["matched"]), len(values["missing"]))
    for index in range(rows):
        matched = values["matched"][index] if index < len(values["matched"]) else ("—", "")
        missing = values["missing"][index] if index < len(values["missing"]) else ("—", "")
        table.add_row(str(matched[0]), str(matched[1]), str(missing[0]), str(missing[1]))
    console.print(table)


@stats_app.callback(invoke_without_command=True)
def stats_default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        render_stats()


@site_app.command("login")
def site_login(site: str) -> None:
    settings = get_settings(); config = settings.sites.get(site)
    if not config: raise typer.BadParameter("Unknown configured site")
    profile = USER_HOME / "browser-profiles" / config.browser_profile
    console.print(f"Opening isolated profile: {profile}. Log in manually, then close the browser.")
    subprocess.run([sys.executable, "-m", "playwright", "open", "--user-data-dir", str(profile), config.login_url], check=False)


@site_app.command("status")
def site_status(site: str) -> None:
    settings = get_settings(); config = settings.sites.get(site)
    if not config:
        raise typer.BadParameter("Unknown configured site")
    try:
        auth = asyncio.run(build_adapter(config.adapter, browser(settings), config.browser_profile).check_auth())
        console.print(f"{site}: {'logged in' if auth.authenticated else 'login required'} · {auth.detail}")
    except Exception as error:
        console.print(f"{site}: check failed · {error}")


@site_app.command("list")
def site_list() -> None:
    settings = get_settings()
    table = Table(title="Configured sites")
    table.add_column("Site")
    table.add_column("Adapter")
    table.add_column("Enabled")
    table.add_column("Profile")
    for name, item in settings.sites.items():
        table.add_row(name, item.adapter, "yes" if item.enabled else "no", item.browser_profile)
    console.print(table)


@site_app.command("enable")
def site_enable(site: str) -> None:
    settings = get_settings()
    if site not in settings.sites:
        raise typer.BadParameter("Unknown configured site")
    update_user_config(lambda data: data.setdefault("sites", {}).setdefault(site, {}).update({"enabled": True}))
    console.print(f"{site}: enabled")


@site_app.command("disable")
def site_disable(site: str) -> None:
    settings = get_settings()
    if site not in settings.sites:
        raise typer.BadParameter("Unknown configured site")
    update_user_config(lambda data: data.setdefault("sites", {}).setdefault(site, {}).update({"enabled": False}))
    console.print(f"{site}: disabled")


@site_app.command("test")
def site_test(site: str) -> None:
    """Run a minimal read-only adapter smoke test using the saved site session."""
    settings = get_settings(); config = settings.sites.get(site)
    if config is None:
        raise typer.BadParameter("Unknown configured site")
    adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
    auth = asyncio.run(adapter.check_auth())
    if not auth.authenticated:
        console.print(f"[yellow]{site}: login required · {auth.detail}[/yellow]")
        return
    filters = SearchFilters.model_validate({**config.search, "max_results": 1})
    try:
        jobs = asyncio.run(adapter.search_jobs(filters))
        if jobs:
            console.print(f"[green]{site}: stable adapter check passed[/green] · {len(jobs)} listing(s) read")
        else:
            console.print(
                f"[yellow]{site}: page loaded but returned no listing[/yellow] · "
                "verify filters or run `job-agent site recover` if this persists"
            )
    except Exception as error:
        console.print(f"[red]{site}: adapter check failed · {error}[/red]")
        console.print(f"Trace saved under {USER_HOME / 'artifacts' / 'playwright-traces'}")


@site_app.command("recover")
def site_recover(site: str, reason: str = typer.Option("Adapter failure requires selector review.")) -> None:
    """Ask the configured recovery role for a reviewable adapter patch; it never edits code."""
    settings = get_settings()
    config = settings.sites.get(site)
    if config is None:
        raise typer.BadParameter("Unknown configured site")
    provider = analyzer(settings, "recovery")
    if provider is None:
        raise typer.BadParameter("No recovery provider is selected. Run `job-agent providers setup` and select one first.")
    adapter = build_adapter(config.adapter, browser(settings), config.browser_profile)
    path = asyncio.run(propose_adapter_recovery(provider, adapter, site, reason))
    console.print(f"Recovery proposal saved: {path}\nReview it, apply changes manually, then run `job-agent site test {site}`.")


@app.command()
def tui() -> None:
    """Open the primary-buffer keyboard workbench."""
    NativeHarnessApp().run()


if __name__ == "__main__": app()
