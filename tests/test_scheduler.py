import plistlib
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from job_agent.config.settings import Settings
from job_agent.database.repositories import Store
from job_agent.scheduler.launchd import (
    LAUNCH_AGENT_LABEL,
    bootstrap_launch_agent,
    launch_agent_status,
    write_launch_agent,
)
from job_agent.scheduler.runtime import HarnessScheduler


def test_scheduler_limits_background_work_to_configured_weekdays_and_hours(tmp_path) -> None:
    settings = Settings.model_validate({
        "app": {"timezone": "Europe/Moscow"},
        "scheduler": {"working_hours": {"weekdays": [0, 1, 2, 3, 4], "start_hour": 9, "end_hour": 19}},
    })
    scheduler = HarnessScheduler(Store(f"sqlite:///{tmp_path / 'state.sqlite3'}"), settings)
    timezone = ZoneInfo("Europe/Moscow")

    assert scheduler.within_working_window(datetime(2026, 7, 27, 10, tzinfo=timezone))
    assert not scheduler.within_working_window(datetime(2026, 7, 27, 19, tzinfo=timezone))
    assert not scheduler.within_working_window(datetime(2026, 7, 26, 10, tzinfo=timezone))


def test_scheduler_refuses_a_second_process_lease(tmp_path) -> None:
    settings = Settings.model_validate({"scheduler": {}})
    lock_path = tmp_path / "scheduler.lock"
    first = HarnessScheduler(Store(f"sqlite:///{tmp_path / 'one.sqlite3'}"), settings, lock_path)
    second = HarnessScheduler(Store(f"sqlite:///{tmp_path / 'two.sqlite3'}"), settings, lock_path)

    first.start()
    try:
        try:
            second.start()
        except RuntimeError as error:
            assert "already running" in str(error)
        else:
            raise AssertionError("second scheduler acquired the active lease")
    finally:
        first.stop()


def test_launch_agent_writer_keeps_runtime_paths_user_owned(tmp_path) -> None:
    user_home = tmp_path / "user-data"
    destination = tmp_path / "LaunchAgents" / "local.job-agent.plist"
    path = write_launch_agent(tmp_path / "venv" / "bin" / "job-agent", tmp_path / "project", user_home, destination)
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["Label"] == LAUNCH_AGENT_LABEL
    assert payload["ProgramArguments"][-2:] == ["scheduler", "serve"]
    assert str(user_home / "logs") in payload["StandardOutPath"]


def test_launch_agent_uses_legacy_load_when_bootstrap_is_rejected(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "bootstrap":
            return subprocess.CompletedProcess(command, 5, "", "bootstrap rejected")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("job_agent.scheduler.launchd.subprocess.run", fake_run)
    bootstrap_launch_agent(tmp_path / "local.job-agent.plist")

    assert calls[0][1] == "bootstrap"
    assert calls[1][1:3] == ["load", "-w"]


def test_launch_agent_status_reports_running_service(monkeypatch) -> None:
    monkeypatch.setattr("job_agent.scheduler.launchd.sys.platform", "darwin")
    monkeypatch.setattr(
        "job_agent.scheduler.launchd.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "\tstate = running\n\tpid = 123\n", ""),
    )

    active, detail = launch_agent_status()

    assert active is True
    assert "state = running" in detail
    assert "pid = 123" in detail
