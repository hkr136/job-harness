from pathlib import Path

from job_agent.database.repositories import Store
from job_agent.models import RawMessage, SendMessageResult
from job_agent.services.message_service import MessageService, classify_message


def test_message_classification_prioritizes_sensitive_requests() -> None:
    assert classify_message("Напишите в Telegram, пожалуйста") == "personal_data_or_external_contact"
    assert classify_message("Сделайте тестовое задание") == "test_task"
    assert classify_message("Сможете сделать это?") == "question"


def test_profile_backed_message_reply_is_draft_only_when_fact_is_known(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    message, _ = store.upsert_message(RawMessage(
        external_message_id="salary", site="sample", conversation_id="c1", sender="Employer",
        body="Какие у вас зарплатные ожидания?", is_unread=True,
    ))
    decision = MessageService(store, None).prepare_reply(message.id, {
        "candidate": {"compensation": {"monthly_min": 100000, "monthly_target": 120000, "currency": "RUB"}}
    })
    assert decision.status == "draft"
    assert "120 000" in decision.draft
    assert store.get_message_reply(message.id).status == "draft"


def test_unknown_message_question_needs_clarification(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    message, _ = store.upsert_message(RawMessage(
        external_message_id="unknown", site="sample", conversation_id="c1", sender="Employer",
        body="Когда сможете выйти на работу?", is_unread=True,
    ))
    decision = MessageService(store, None).prepare_reply(message.id, {"candidate": {}})
    assert decision.status == "needs_clarification"
    assert store.get_message_reply(message.id).reason


def test_remote_profile_can_decline_mandatory_relocation(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    message, _ = store.upsert_message(RawMessage(
        external_message_id="relocation", site="sample", conversation_id="c1", sender="Employer",
        body="Работа в офисе в другой стране, нужна релокация. Интересно?", is_unread=True,
    ))
    decision = MessageService(store, None).prepare_reply(message.id, {
        "candidate": {"preferred_work": {"formats": ["remote", "contract"]}}
    })
    assert decision.status == "draft"
    assert "удалённый" in decision.draft


class ConfirmingMessageAdapter:
    class capabilities:
        send_messages = True

    async def send_message(self, conversation_id: str, text: str, confirm: bool = False) -> SendMessageResult:
        assert conversation_id == "c1"
        assert text
        assert confirm
        return SendMessageResult(success=True, confirmed=True, detail="Visible chat confirmation")


async def test_message_send_persists_only_confirmed_delivery(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    message, _ = store.upsert_message(RawMessage(
        external_message_id="city", site="sample", conversation_id="c1", sender="Employer",
        body="В каком вы городе?", is_unread=True,
    ))
    service = MessageService(store, ConfirmingMessageAdapter())
    service.prepare_reply(message.id, {"candidate": {"location": {"city": "Москва"}}})
    result = await service.send_prepared_reply(message.id, confirm=True)
    assert result.confirmed
    assert store.get_message_reply(message.id).status == "sent"


class InboxAdapter:
    async def get_unread_messages(self) -> list[RawMessage]:
        return [RawMessage(
            external_message_id="inbox-1", site="sample", conversation_id="c1", sender="Employer",
            body="Какие у вас зарплатные ожидания?", is_unread=True,
        )]


async def test_collect_new_unread_returns_only_new_local_records(tmp_path: Path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    service = MessageService(store, InboxAdapter())

    first = await service.collect_new_unread()
    second = await service.collect_new_unread()

    assert len(first) == 1
    assert second == []
