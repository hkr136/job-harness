from pathlib import Path

from job_agent.database.repositories import Store
from job_agent.models import RawJobDetails, RawMessage


def test_job_deduplication(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    raw = RawJobDetails(external_job_id="42", site="sample", url="https://example.test/jobs/42", title="Sample role", description="Sample description")
    _, first = store.upsert_job(raw); _, second = store.upsert_job(raw)
    assert first is True and second is False


def test_local_conversation_summary(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    store.upsert_message(RawMessage(external_message_id="1", site="sample", conversation_id="c1", sender="Employer", body="Hello", is_unread=True))
    store.upsert_message(RawMessage(external_message_id="2", site="sample", conversation_id="c1", sender="Employer", body="Follow-up", is_unread=False))
    conversations = store.list_conversations()
    assert conversations[0][0] == "sample:c1"
    assert conversations[0][2] == 1
    assert len(store.list_conversation_messages("sample", "c1")) == 2


def test_confirmed_application_updates_job_funnel_state(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    job, _ = store.upsert_job(
        RawJobDetails(external_job_id="43", site="sample", url="https://example.test/jobs/43", title="Sample role", description="Sample description")
    )
    store.save_draft(job.id, "sample", "Hello")
    store.confirm_application_submission(job.id, "43", "Site confirmation observed")

    assert store.get_job(job.id)[0].status == "applied"
    assert store.stats()["submitted"] == 1


def test_unread_previews_from_the_same_conversation_keep_one_canonical_item(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    first, created = store.upsert_message(RawMessage(
        external_message_id="preview-one", site="sample", conversation_id="chat-1", sender="Employer", body="Hello", is_unread=True
    ))
    assert created
    store.save_message_reply(first.id, "Draft", "draft")

    second, created = store.upsert_message(RawMessage(
        external_message_id="preview-two", site="sample", conversation_id="chat-1", sender="Employer", body="Hello\n1", is_unread=True
    ))

    assert not created
    assert second.id == first.id
    assert len(store.list_messages(unread_only=True)) == 1
