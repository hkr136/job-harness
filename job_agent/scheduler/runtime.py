from __future__ import annotations

import fcntl
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from job_agent.config.settings import USER_HOME, Settings
from job_agent.database.repositories import Store
from job_agent.services.harness import HarnessWorker


class HarnessScheduler:
    """In-process scheduler. Durable work remains in SQLite, not APScheduler memory."""

    def __init__(self, store: Store, settings: Settings, lock_path: Path | None = None) -> None:
        self.store, self.settings = store, settings
        self.worker = HarnessWorker(store, settings)
        self.scheduler = BackgroundScheduler(timezone=settings.app.timezone)
        self._configured = False
        self._lock_path = lock_path or USER_HOME / "scheduler.lock"
        self._lock_handle = None

    def _acquire_lease(self) -> None:
        if self._lock_handle is not None:
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("Another Job Agent scheduler is already running. Use the LaunchAgent service or stop the other scheduler first.") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_handle = handle

    def _release_lease(self) -> None:
        if self._lock_handle is None:
            return
        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        self._lock_handle.close()
        self._lock_handle = None

    def configure(self) -> None:
        if self._configured:
            return
        search = self.settings.scheduler.get("jobs", {}).get("search", {})
        interval = int(search.get("interval_minutes", 30))
        if search.get("enabled", True):
            self.scheduler.add_job(self.enqueue_scans, "interval", minutes=interval, id="enqueue_scans", max_instances=1, coalesce=True)
        messages = self.settings.scheduler.get("jobs", {}).get("messages", {})
        if messages.get("enabled", True):
            self.scheduler.add_job(self.enqueue_messages, "interval", minutes=int(messages.get("interval_minutes", 10)), id="enqueue_messages", max_instances=1, coalesce=True)
        statuses = self.settings.scheduler.get("jobs", {}).get("application_statuses", {})
        if statuses.get("enabled", False):
            self.scheduler.add_job(self.enqueue_statuses, "interval", minutes=int(statuses.get("interval_minutes", 60)), id="enqueue_statuses", max_instances=1, coalesce=True)
        self.scheduler.add_job(self.worker.process_one_sync, "interval", seconds=5, id="process_queue", max_instances=1, coalesce=True)
        self._configured = True

    def within_working_window(self, now: datetime | None = None) -> bool:
        """Respect user-owned weekday/hour limits before enqueueing remote work.

        Manual CLI commands intentionally bypass this check.  The scheduler is
        for background polling, where an unexpectedly overnight browser run is
        both noisy and unfriendly to the job platforms.
        """
        window = self.settings.scheduler.get("working_hours", {})
        timezone = ZoneInfo(self.settings.app.timezone)
        current = (now or datetime.now(timezone)).astimezone(timezone)
        weekdays = window.get("weekdays", [0, 1, 2, 3, 4])
        try:
            allowed_days = {int(day) for day in weekdays}
        except (TypeError, ValueError):
            allowed_days = {0, 1, 2, 3, 4}
        if current.weekday() not in allowed_days:
            return False
        start = int(window.get("start_hour", 9))
        end = int(window.get("end_hour", 19))
        return start <= current.hour < end

    def enqueue_scans(self) -> None:
        if not self.within_working_window():
            return
        for name, config in self.settings.sites.items():
            if config.enabled and config.automation.get("scan_enabled", True):
                self.worker.enqueue_scan(name)

    def enqueue_messages(self) -> None:
        if not self.within_working_window():
            return
        for name, config in self.settings.sites.items():
            if config.enabled and config.automation.get("messages_enabled", False):
                self.worker.enqueue_message_check(name)

    def enqueue_statuses(self) -> None:
        if not self.within_working_window():
            return
        for name, config in self.settings.sites.items():
            if config.enabled and config.automation.get("status_enabled", True):
                self.worker.enqueue_status_check(name)

    def start(self) -> None:
        self._acquire_lease()
        self.store.requeue_stale_running_tasks()
        self.store.fail_stale_running_runs()
        self.configure()
        if not self.scheduler.running:
            self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self._release_lease()

    @property
    def running(self) -> bool:
        return self.scheduler.running

    def summary(self) -> str:
        jobs = self.scheduler.get_jobs() if self._configured else []
        return f"{'running' if self.running else 'stopped'} · {len(jobs)} scheduled jobs"
