from job_agent.config.settings import Settings
from job_agent.database.repositories import Store
from job_agent.models import RawMessage
from job_agent.services.orchestration import OrchestrationService, ToolCall, ToolPolicyGate


def test_policy_gate_blocks_external_calls_in_safe_and_kwork_external_channel() -> None:
    gate = ToolPolicyGate()
    call = ToolCall("send_internal_message", {"id": 4, "channel": "internal"}, "Send prepared reply.")
    assert gate.allow(call, "safe", "hh")[0] is False
    assert gate.allow(call, "armed", "hh")[0] is True
    forbidden = ToolCall("send_internal_message", {"id": 4, "channel": "telegram"}, "Move chat externally.")
    assert gate.allow(forbidden, "armed", "kwork")[0] is False
    invalid_status = ToolCall("set_job_status", {"id": 4, "status": "submitted"}, "Invent an invalid local state.")
    assert gate.allow(invalid_status, "safe")[0] is False


async def test_message_agent_persists_tool_transcript(monkeypatch, tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    message, _ = store.upsert_message(
        RawMessage(external_message_id="m-1", site="hh", conversation_id="c-1", sender="Employer", body="Здравствуйте, расскажите подробнее")
    )
    monkeypatch.setattr("job_agent.services.orchestration.load_profile", lambda: {"candidate": {}})

    detail = await OrchestrationService(store, Settings(), mode="safe").handle_message(message.id)

    assert "Reply decision" in detail
    events = store.list_agent_events("message", message.id)
    assert {event.event_type for event in events} >= {"agent_start", "tool_execution_start", "tool_execution_end"}
    assert store.get_message_reply(message.id).status == "needs_clarification"


async def test_chat_persists_user_and_assistant_transcript(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")

    session_id, reply = await OrchestrationService(store, Settings(), mode="safe", use_llm=False).handle_chat("Что сейчас требует ответа?")

    assert "LLM orchestration" in reply
    events = store.list_agent_events("chat", session_id)
    assert {event.event_type for event in events} >= {"agent_start", "user_message", "assistant_message"}


async def test_unknown_chat_tool_id_becomes_a_safe_tool_result(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    service = OrchestrationService(store, Settings(), mode="safe", use_llm=False)
    session = store.start_agent_session("chat", None, "safe")

    detail = await service.execute(
        session.id, "chat", session.id, None,
        ToolCall("read_message", {"id": 24}, "Read a message that was listed earlier."),
    )

    assert "was not run" in detail
    assert "Unknown message ID: 24" in detail


def test_chat_context_ignores_a_failed_stale_id(tmp_path) -> None:
    """A failed model guess must not become the target of the next chat ask."""
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    service = OrchestrationService(store, Settings(), mode="safe", use_llm=False)
    session = store.start_agent_session("chat", None, "safe")
    store.add_agent_event(session.id, "chat", session.id, "tool_execution_end", "read_job", payload={"id": 28}, detail='{"vacancy": {"id": 28}}')
    store.add_agent_event(
        session.id,
        "chat",
        session.id,
        "tool_execution_end",
        "read_application",
        payload={"id": 69},
        detail="Tool read_application was not run: No draft exists for job #69.",
    )

    assert service._last_context_id(session.id, "job") == 28


async def test_chat_message_write_requires_a_read_in_the_current_turn(monkeypatch, tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    message, _ = store.upsert_message(
        RawMessage(external_message_id="m-2", site="hh", conversation_id="c-2", sender="Employer", body="Здравствуйте")
    )
    monkeypatch.setattr("job_agent.services.orchestration.load_profile", lambda: {"candidate": {}})
    service = OrchestrationService(store, Settings(), mode="safe", use_llm=False)
    session = store.start_agent_session("chat", None, "safe")
    store.add_agent_event(session.id, "chat", session.id, "user_message", detail="Подготовь ответ")

    blocked = await service.execute(
        session.id, "chat", session.id, None,
        ToolCall("create_reply_draft", {"id": message.id}, "Prepare a reply."),
    )
    assert "read message" in blocked
    assert "chat-session ID" in blocked

    read = await service.execute(
        session.id, "chat", session.id, None,
        ToolCall("read_message", {"id": message.id}, "Read the message."),
    )
    assert '"id": ' + str(message.id) in read

    prepared = await service.execute(
        session.id, "chat", session.id, None,
        ToolCall("create_reply_draft", {"id": message.id}, "Prepare a reply."),
    )
    assert "Reply decision" in prepared


async def test_chat_scan_without_site_is_blocked_before_executor(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    service = OrchestrationService(store, Settings(), mode="safe", use_llm=False)
    session = store.start_agent_session("chat", None, "safe")

    detail = await service.execute(session.id, "chat", session.id, None, ToolCall("enqueue_scan", {}, "Look for jobs."))

    assert "choose an enabled site" in detail


async def test_kwork_scan_request_is_scoped_and_safe_queues_only(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    settings = Settings.model_validate({"sites": {"kwork": {"adapter": "kwork", "browser_profile": "kwork", "login_url": "https://kwork.ru"}}})

    session_id, reply = await OrchestrationService(store, settings, mode="safe", use_llm=False).handle_chat("Сканируй Kwork")

    assert "KWORK RESULT" in reply
    assert "SAFE mode" in reply
    events = store.list_agent_events("chat", session_id)
    names = {event.tool_name for event in events}
    assert names >= {"enqueue_scan", "enqueue_message_check"}
    assert "create_reply_draft" not in names


async def test_chat_message_check_queues_every_enabled_inbox_in_safe_mode(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    settings = Settings.model_validate({
        "sites": {
            "hh": {"adapter": "hh", "browser_profile": "hh", "login_url": "https://hh.ru"},
            "kwork": {"adapter": "kwork", "browser_profile": "kwork", "login_url": "https://kwork.ru"},
        }
    })

    session_id, reply = await OrchestrationService(store, settings, mode="safe", use_llm=False).handle_chat("Есть новые сообщения в чатах?")

    assert "MESSAGE CHECK" in reply
    assert "hh" in reply and "kwork" in reply
    events = store.list_agent_events("chat", session_id)
    assert sum(event.tool_name == "enqueue_message_check" and event.event_type == "tool_execution_end" for event in events) == 2


def test_chat_visible_result_summary_lists_scanned_vacancies_and_draft() -> None:
    result = OrchestrationService._visible_result_summary([
        'kwork: {"new": 1, "analyzed": 1, "found": [{"id": 72, "title": "Python bot", "score": 85}]}',
        "Reply decision: draft.\nDraft:\nЗдравствуйте!",
    ])

    assert "#72 · 85 · Python bot" in result
    assert "Создан локальный черновик" in result


def test_failed_tool_receipt_never_claims_a_draft_was_created() -> None:
    reply = OrchestrationService._confirmed_failure_reply([
        'hh: {"new": 0, "analyzed": 4, "found": []}',
        "Tool create_reply_draft was not run: Unknown message ID: 46.",
    ])

    assert "Сканирование подтверждено" in reply
    assert "не выполнено" in reply
    assert "создан" not in reply
