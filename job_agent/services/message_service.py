from __future__ import annotations

from dataclasses import dataclass

from job_agent.config.settings import compensation_answer
from job_agent.database.models import MessageRecord
from job_agent.database.repositories import Store
from job_agent.models import SendMessageResult
from job_agent.sites.base import BaseSiteAdapter


def classify_message(body: str) -> str:
    """Classify a message conservatively; classification never authorizes a send."""
    text = body.lower()
    if any(token in text for token in ("http://", "https://", "t.me/", "telegram", "телефон", "почт")):
        return "personal_data_or_external_contact"
    if any(token in text for token in ("тестов", "test task", "тестовое")):
        return "test_task"
    if any(token in text for token in ("созвон", "звонок", "встреч", "интервью")):
        return "invitation"
    if any(token in text for token in ("отказ", "не подойд", "закрыли вакансию")):
        return "rejection"
    if "?" in body or any(token in text for token in ("уточн", "расскаж", "можете", "сможете")):
        return "question"
    return "reply"


@dataclass(frozen=True)
class ReplyDecision:
    status: str
    draft: str
    reason: str = ""


def decide_profile_backed_reply(message: MessageRecord, profile: dict) -> ReplyDecision:
    """Return only a deterministic reply whose every fact is user-owned.

    Anything requiring interpretation, a commitment, a test task, a schedule,
    legal status, or an undisclosed profile fact becomes a local review item.
    This function never contacts a site and intentionally stays narrower than an
    LLM writer; safety is the gate before an adapter may send.
    """
    text = message.body.lower()
    if message.category == "test_task":
        return ReplyDecision("needs_clarification", "", "Test task requires an explicit user decision.")
    if message.category == "invitation":
        return ReplyDecision("needs_clarification", "", "Meeting time or interview commitment requires an explicit user decision.")
    if message.category == "rejection":
        return ReplyDecision("not_needed", "", "Employer message is an explicit rejection.")
    if message.category == "personal_data_or_external_contact":
        if message.site == "kwork":
            return ReplyDecision("draft", "Здравствуйте! Давайте продолжим общение здесь, во внутреннем чате Kwork.")
        return ReplyDecision("needs_clarification", "", "External contact or personal data request requires a user decision.")

    candidate = profile.get("candidate", {}) if isinstance(profile, dict) else {}
    preferred_work = candidate.get("preferred_work", {}) if isinstance(candidate, dict) else {}
    formats = preferred_work.get("formats", []) if isinstance(preferred_work, dict) else preferred_work
    normalized_formats = {str(item).lower() for item in formats} if isinstance(formats, list) else set()
    if any(token in text for token in ("релокац", "работа в офисе", "офис в ", "переезд")) and "remote" in normalized_formats:
        return ReplyDecision(
            "draft",
            "Здравствуйте! Спасибо за предложение. Рассматриваю удалённый формат, поэтому позиция с обязательной релокацией или офисной работой мне не подойдёт.",
        )
    if any(token in text for token in ("зарплат", "доход", "ожидани", "компенсац")):
        answer = compensation_answer(profile)
        if answer:
            return ReplyDecision("draft", f"Здравствуйте! {answer}")
        return ReplyDecision("needs_clarification", "", "Compensation expectations are not present in the profile.")
    if any(token in text for token in ("город", "локац", "местонахожд")):
        location = candidate.get("location", {}) if isinstance(candidate, dict) else {}
        city = location.get("city") if isinstance(location, dict) else None
        if city:
            return ReplyDecision("draft", f"Здравствуйте! Мой текущий город — {city}. Рассматриваю удалённую работу.")
        return ReplyDecision("needs_clarification", "", "Current location is not present in the profile.")
    if any(token in text for token in ("полную занятость", "полная занятость", "гпх", "договор")):
        availability = candidate.get("availability", {}) if isinstance(candidate, dict) else {}
        employment = availability.get("employment") if isinstance(availability, dict) else None
        contracts = availability.get("contract_options", []) if isinstance(availability, dict) else []
        if employment or contracts:
            values = [str(employment)] if employment else []
            values.extend(str(item) for item in contracts if item)
            return ReplyDecision("draft", f"Здравствуйте! Рассматриваю формат: {', '.join(values)}.")
        return ReplyDecision("needs_clarification", "", "Employment preferences are not present in the profile.")
    return ReplyDecision("needs_clarification", "", "Question needs a fact or commitment that is not safely determined from the profile.")


class MessageService:
    def __init__(self, store: Store, adapter: BaseSiteAdapter | None) -> None:
        self.store, self.adapter = store, adapter

    async def collect_unread(self) -> int:
        return len(await self.collect_new_unread())

    async def collect_new_unread(self) -> list[MessageRecord]:
        if self.adapter is None:
            raise ValueError("A site adapter is required to collect remote messages")
        created: list[MessageRecord] = []
        for message in await self.adapter.get_unread_messages():
            message.category = classify_message(message.body)
            record, is_new = self.store.upsert_message(message)
            if is_new:
                created.append(record)
        return created

    def prepare_reply(self, message_id: int, profile: dict) -> ReplyDecision:
        message = self.store.get_message(message_id)
        decision = decide_profile_backed_reply(message, profile)
        self.store.save_message_reply(message_id, decision.draft, decision.status, decision.reason)
        return decision

    async def send_prepared_reply(self, message_id: int, confirm: bool = False) -> SendMessageResult:
        """Send only a previously prepared draft through a capable site adapter.

        Explicit confirmation is required here and again in every adapter.  The
        local status remains a draft if the site cannot prove delivery.
        """
        if not confirm:
            return SendMessageResult(success=False, confirmed=False, detail="Explicit confirmation is required before sending a message reply.")
        if self.adapter is None or not self.adapter.capabilities.send_messages:
            return SendMessageResult(success=False, confirmed=False, detail="This site adapter does not support confirmed message sending yet.")
        reply = self.store.get_message_reply(message_id)
        if reply.status != "draft" or not reply.draft.strip():
            return SendMessageResult(success=False, confirmed=False, detail="Only a non-empty prepared draft may be sent.")
        result = await self.adapter.send_message(reply.conversation_id, reply.draft, confirm=True)
        if result.success and result.confirmed:
            self.store.confirm_message_reply_sent(message_id, reply.draft, result.detail)
        return result
