import asyncio
from datetime import timedelta

from job_agent.config.settings import LimitSettings
from job_agent.database.repositories import Store
from job_agent.services.harness import HarnessWorker


def test_queue_is_idempotent_and_retries(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    first = store.enqueue_task("scan", "sample", {}, "scan:sample")
    second = store.enqueue_task("scan", "sample", {}, "scan:sample")
    assert first.id == second.id

    claimed = store.claim_next_task()
    assert claimed is not None and claimed.status == "running"
    store.retry_task(claimed.id, "temporary failure")
    task = store.list_tasks()[0]
    assert task.status == "queued"
    assert task.attempts == 1


def test_queue_completes_claimed_task(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    created = store.enqueue_task("scan", "sample", {}, "scan:sample")
    claimed = store.claim_next_task()
    assert claimed is not None
    store.finish_task(created.id, "ok")
    task = store.list_tasks()[0]
    assert task.status == "completed"
    assert task.last_error == "ok"


def test_queue_reuses_terminal_task_for_the_next_schedule_cycle(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    first = store.enqueue_task("scan", "sample", {"round": 1}, "scan:sample")
    claimed = store.claim_next_task()
    assert claimed is not None
    store.finish_task(first.id, "first scan completed")

    next_cycle = store.enqueue_task("scan", "sample", {"round": 2}, "scan:sample")

    assert next_cycle.id == first.id
    assert next_cycle.status == "queued"
    assert next_cycle.attempts == 0
    assert '"round": 2' in next_cycle.payload_json


def test_queue_recovers_stale_running_tasks(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    created = store.enqueue_task("scan", "sample", {}, "scan:sample")
    claimed = store.claim_next_task()
    assert claimed is not None

    # A zero-duration threshold makes the test independent of wall-clock time.
    assert store.requeue_stale_running_tasks(after=timedelta(seconds=0)) == 1
    recovered = store.list_tasks()[0]
    assert recovered.id == created.id
    assert recovered.status == "queued"
    assert "Recovered stale" in recovered.last_error


def test_queue_marks_stale_run_history_as_interrupted(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    run = store.start_run("scan", "sample")

    assert store.fail_stale_running_runs(after=timedelta(seconds=0)) == 1
    recovered = store.get_run(run.id)
    assert recovered.status == "interrupted"
    assert recovered.finished_at is not None
    assert "Recovered after worker restart" in recovered.detail


def test_remote_task_timeout_has_a_conservative_default() -> None:
    assert LimitSettings().remote_task_timeout_seconds == 240


async def test_manual_run_is_recorded_without_using_queue(monkeypatch, tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    worker = HarnessWorker(store, type("Settings", (), {"limits": LimitSettings()})())

    async def fake_execute(kind: str, site: str | None) -> str:
        assert (kind, site) == ("scan", "sample")
        return "ok"

    monkeypatch.setattr(worker, "_execute_task", fake_execute)
    assert await worker.run_now("scan", "sample") == "ok"
    assert not store.list_tasks()


async def test_timeout_is_recorded_with_site_kind_and_limit(monkeypatch, tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    worker = HarnessWorker(store, type("Settings", (), {"limits": LimitSettings(remote_task_timeout_seconds=0)})())
    store.enqueue_task("scan", "sample", {}, "scan:sample")

    async def wait_forever(kind: str, site: str | None) -> str:
        await asyncio.sleep(1)
        return "never"

    monkeypatch.setattr(worker, "_execute_task", wait_forever)
    result = await worker.process_one()
    assert result and "sample scan timed out after 0s" in result
    assert "sample scan timed out after 0s" in store.list_runs()[0].detail
