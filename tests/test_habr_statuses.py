import pytest

from job_agent.models import PreparedApplication
from job_agent.sites.geekjob.adapter import GeekJobAdapter
from job_agent.sites.habr.adapter import HabrAdapter
from job_agent.sites.hh.adapter import HHAdapter


def test_habr_response_status_mapping() -> None:
    assert HabrAdapter.response_status("Python Developer\nНе прочитано") == "submitted"
    assert HabrAdapter.response_status("Python Developer\nПрочитано") == "viewed"
    assert HabrAdapter.response_status("Python Developer") == "submitted"


def test_habr_conversation_reader_keeps_only_latest_inbound_threads() -> None:
    rows = HabrAdapter.parse_conversation_rows([
        {"conversation_id": "employer", "sender": "Employer", "preview": "Please share your availability"},
        {"conversation_id": "answered", "sender": "Employer", "preview": "Вы: Thank you, I am interested."},
        {"conversation_id": "empty", "sender": "Employer", "preview": ""},
    ])
    assert rows == [{"conversation_id": "employer", "sender": "Employer", "preview": "Please share your availability"}]


def test_geekjob_response_status_mapping() -> None:
    assert GeekJobAdapter.response_status("Статус: отправлен запрос") == "submitted"
    assert GeekJobAdapter.response_status("Статус: прочитано") == "viewed"
    assert GeekJobAdapter.response_status("Статус: приглашение на интервью") == "interview"


@pytest.mark.asyncio
async def test_habr_submission_requires_explicit_confirmation() -> None:
    adapter = HabrAdapter(None)  # The dry-run gate must execute before browser access.
    result = await adapter.submit_application(PreparedApplication(job_id=1, site="habr", external_job_id="1", body="Text"), confirm=False)
    assert not result.confirmed
    assert "Dry run" in result.detail


@pytest.mark.asyncio
async def test_geekjob_submission_requires_explicit_confirmation() -> None:
    adapter = GeekJobAdapter(None)  # The dry-run gate must execute before browser access.
    result = await adapter.submit_application(PreparedApplication(job_id=1, site="geekjob", external_job_id="1", body="Text"), confirm=False)
    assert not result.confirmed
    assert "Dry run" in result.detail


@pytest.mark.asyncio
async def test_hh_submission_requires_explicit_confirmation() -> None:
    adapter = HHAdapter(None)  # The dry-run gate must execute before browser/profile access.
    result = await adapter.submit_application(PreparedApplication(job_id=1, site="hh", external_job_id="1", body="Text"), confirm=False)
    assert not result.confirmed
    assert "Dry run" in result.detail


@pytest.mark.asyncio
async def test_hh_submission_rejects_blank_cover_letter_before_browser_access() -> None:
    adapter = HHAdapter(None)
    result = await adapter.submit_application(
        PreparedApplication(job_id=1, site="hh", external_job_id="1", body=""), confirm=True
    )
    assert not result.confirmed
    assert "cover letter" in result.detail


@pytest.mark.asyncio
async def test_hh_message_send_requires_explicit_confirmation_before_browser_access() -> None:
    adapter = HHAdapter(None)
    result = await adapter.send_message("42", "Здравствуйте!", confirm=False)
    assert not result.confirmed
    assert "Explicit confirmation" in result.detail


@pytest.mark.asyncio
async def test_habr_message_send_requires_explicit_confirmation_before_browser_access() -> None:
    adapter = HabrAdapter(None)
    result = await adapter.send_message("employer", "Hello", confirm=False)
    assert not result.confirmed
    assert "Explicit confirmation" in result.detail


def test_hh_form_creates_clarifications_only_for_unknown_required_data() -> None:
    payload = {
        "text": [
            {"name": "task_city", "question": "Укажите актуальный город проживания"},
            {"name": "task_github", "question": "Если у Вас есть примеры вашего кода на github, пожалуйста, поделитесь ссылкой"},
            {"name": "task_experience", "question": "Есть ли у Вас опыт коммерческой разработки на Python и какой?"},
            {"name": "task_salary", "question": "Пожалуйста, укажите ваши зарплатные ожидания на данной позиции - минимум и комфорт"},
        ],
        "radios": [{
            "name": "task_military",
            "question": "Есть ли у вас военный билет или приписное свидетельство?",
            "options": ["Есть военный билет", "Я женщина"],
        }],
    }
    fields = HHAdapter.response_form_fields(payload)
    profile = {
        "candidate": {
            "location": {"city": "Moscow"},
            "compensation": {"monthly_min": 180000, "monthly_target": 200000, "currency": "RUB"},
        }
    }
    items = HHAdapter.required_form_clarifications(fields, profile)
    assert [item.field_name for item in items] == ["task_github", "task_experience", "task_military"]
    military = items[-1]
    assert military.kind == "military_status"
    assert "Есть военный билет" in military.question


def test_hh_chat_reader_requires_an_explicit_unread_badge() -> None:
    messages = HHAdapter.parse_unread_chats([
        {"chat_id": "1", "sender": "Employer", "body": "Прочитанное сообщение", "unread": ""},
        {"chat_id": "2", "sender": "Employer", "body": "Новое сообщение", "unread": "1"},
    ])
    assert len(messages) == 1
    assert messages[0].conversation_id == "2"
    assert messages[0].body == "Новое сообщение"
    assert messages[0].external_message_id
