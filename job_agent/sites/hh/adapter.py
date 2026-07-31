from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import quote_plus

from job_agent.browser.manager import BrowserManager
from job_agent.browser.selector_recovery import SelectorRecovery
from job_agent.config.settings import load_profile
from job_agent.models import (
    AuthStatus,
    ClarificationInput,
    ExternalApplicationStatus,
    PreparedApplication,
    RawJob,
    RawJobDetails,
    RawMessage,
    SearchFilters,
    SendMessageResult,
    SubmissionResult,
)
from job_agent.services.form_answers import answer_known_application_question, classify_question
from job_agent.sites.base import BaseSiteAdapter, SiteCapabilities


class HHAdapter(BaseSiteAdapter):
    site_name = "hh"
    capabilities = SiteCapabilities(search_jobs=True, submit_application=True, read_messages=True, send_messages=True, application_statuses=True)

    def __init__(self, browser: BrowserManager, profile: str = "hh") -> None:
        self.browser, self.profile = browser, profile
        self.selector_recovery = SelectorRecovery(self.site_name)

    @staticmethod
    def required_form_clarifications(fields: list[dict[str, object]], profile: dict, vacancy_answers: dict[str, str] | None = None) -> list[ClarificationInput]:
        """Turn only required unknown HH form fields into durable clarification items.

        The submit adapter can call this after it has read visible form labels.  It
        deliberately does not guess radio/select values such as military status.
        """
        items: list[ClarificationInput] = []
        for field in fields:
            question = str(field.get("question", "")).strip()
            required = field.get("required", True)
            if not question or required is False or str(required).lower() == "false":
                continue
            field_name = str(field.get("field_name", ""))
            if (vacancy_answers or {}).get(field_name) or answer_known_application_question(question, profile) is not None:
                continue
            options = [str(option) for option in field.get("options", []) if str(option).strip()]
            if options:
                question = f"{question}\nВарианты: {'; '.join(options)}"
            items.append(ClarificationInput(
                question=question,
                kind=classify_question(question),
                field_name=field_name,
                source="hh_application_form",
                artifact_path=str(field.get("artifact_path") or "") or None,
            ))
        return items

    @staticmethod
    def response_form_fields(payload: dict[str, object]) -> list[dict[str, object]]:
        """Normalize visible HH controls collected from the native response form."""
        fields: list[dict[str, object]] = []
        for item in payload.get("text", []):
            if not isinstance(item, dict) or not item.get("name") or not item.get("question"):
                continue
            fields.append({"field_name": str(item["name"]), "question": str(item["question"]), "required": True, "type": "text"})
        for item in payload.get("radios", []):
            if not isinstance(item, dict) or not item.get("name") or not item.get("question"):
                continue
            fields.append({
                "field_name": str(item["name"]),
                "question": str(item["question"]),
                "required": True,
                "type": "radio",
                "options": item.get("options", []),
            })
        return fields

    @staticmethod
    def field_answer(field: dict[str, object], profile: dict, vacancy_answers: dict[str, str]) -> str | None:
        field_name = str(field.get("field_name", ""))
        if field_name in vacancy_answers:
            return vacancy_answers[field_name]
        return answer_known_application_question(str(field.get("question", "")), profile)

    async def submit_application(self, prepared_application: PreparedApplication, confirm: bool = False) -> SubmissionResult:
        """Safely handle HH's per-vacancy questions before a response can be sent.

        Unknown required data produces clarification items and a screenshot.  No
        fields are filled and no response button is pressed in that branch.
        """
        if not confirm:
            return SubmissionResult(success=False, confirmed=False, detail="Dry run only. Repeat with explicit confirmation.")
        if not prepared_application.external_job_id:
            return SubmissionResult(success=False, confirmed=False, detail="HH vacancy ID is required.")
        if not prepared_application.body.strip():
            return SubmissionResult(
                success=False,
                confirmed=False,
                detail="HH response requires a prepared personalized cover letter; no blank response was sent.",
            )
        profile = load_profile()
        vacancy_url = f"https://hh.ru/vacancy/{prepared_application.external_job_id}"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self.browser.goto(page, vacancy_url)
                response_hrefs = await page.locator('a[data-qa="vacancy-response-link-top"]').evaluate_all(
                    "els => [...new Set(els.map(el => el.getAttribute('href')).filter(Boolean))]"
                )
                if not response_hrefs:
                    page_text = await page.locator("main").inner_text() if await page.locator("main").count() else ""
                    if "Отклик" in page_text and ("отправлен" in page_text.lower() or "откликнулись" in page_text.lower()):
                        return SubmissionResult(success=False, confirmed=False, detail="The site already shows an existing response; duplicate was not sent.")
                    return SubmissionResult(success=False, confirmed=False, detail="HH response entry point was not found.")
                response_url = str(response_hrefs[0])
                if response_url.startswith("/"):
                    response_url = f"https://hh.ru{response_url}"
                await self.browser.goto(page, response_url)
                form = page.locator('form[name="vacancy_response"]')
                if await form.count() != 1:
                    screenshot = await self.browser.screenshot(page, "hh-response-form-missing")
                    return SubmissionResult(success=False, confirmed=False, detail="HH response form was not found.", screenshot_path=screenshot)
                payload = await form.evaluate("""form => {
                    const cleanQuestion = (node, placeholder) => {
                        let current = node;
                        for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
                            const lines = (current.innerText || '').split('\\n').map(line => line.trim()).filter(Boolean);
                            const useful = lines.filter(line => line !== placeholder);
                            if (useful.length && useful.join(' ').length > 12) return useful.join(' ');
                        }
                        return '';
                    };
                    const text = [...form.querySelectorAll('textarea[name]')].map(element => ({
                        name: element.name,
                        question: cleanQuestion(element, 'Писать тут'),
                    }));
                    const groups = new Map();
                    for (const element of form.querySelectorAll('input[type="radio"][name]')) {
                        if (!groups.has(element.name)) groups.set(element.name, []);
                        groups.get(element.name).push(element);
                    }
                    const radios = [...groups.entries()].map(([name, elements]) => {
                        const options = elements.map(element => {
                            const label = element.closest('label') || element.parentElement?.parentElement?.parentElement;
                            return (label?.innerText || '').trim();
                        }).filter(Boolean);
                        let current = elements[0];
                        let question = '';
                        for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
                            const contained = [...current.querySelectorAll(`input[type="radio"][name="${name}"]`)];
                            if (contained.length < 2) continue;
                            const lines = (current.innerText || '').split('\\n').map(line => line.trim()).filter(Boolean);
                            const useful = lines.filter(line => !options.includes(line));
                            if (useful.length) {
                                question = useful.join(' ');
                                break;
                            }
                        }
                        return {name, question, options};
                    });
                    return {text, radios};
                }""")
                fields = self.response_form_fields(payload)
                screenshot = await self.browser.screenshot(page, "hh-response-form")
                missing = self.required_form_clarifications(fields, profile, prepared_application.form_answers)
                if missing:
                    missing = [item.model_copy(update={"artifact_path": screenshot}) for item in missing]
                    return SubmissionResult(
                        success=False,
                        confirmed=False,
                        detail="HH requires profile facts that are not confirmed; no form fields were filled.",
                        screenshot_path=screenshot,
                        clarifications=missing,
                    )
                for field in fields:
                    answer = self.field_answer(field, profile, prepared_application.form_answers)
                    if not answer:
                        return SubmissionResult(success=False, confirmed=False, detail="HH required answer disappeared before submission.", screenshot_path=screenshot)
                    field_name = str(field["field_name"])
                    if field.get("type") == "radio":
                        radio = page.locator(f'input[type="radio"][name="{field_name}"]')
                        options = await radio.evaluate_all("els => els.map(el => ({value: el.value, label: (el.closest('label') || el.parentElement?.parentElement?.parentElement)?.innerText?.trim() || ''}))")
                        selected = next((item for item in options if str(item["label"]).strip() == answer.strip()), None)
                        if selected is None:
                            return SubmissionResult(
                                success=False,
                                confirmed=False,
                                detail="HH radio answer must exactly match one of the saved form options.",
                                screenshot_path=screenshot,
                                clarifications=[ClarificationInput(
                                    question=f"{field['question']}\\nВарианты: {'; '.join(str(item['label']) for item in options)}",
                                    kind=classify_question(str(field["question"])),
                                    field_name=field_name,
                                    source="hh_application_form",
                                    artifact_path=screenshot,
                                )],
                            )
                        await radio.locator(f'[value="{selected["value"]}"]').check()
                    else:
                        await page.locator(f'textarea[name="{field_name}"]').fill(answer)

                # HH hides the personalized letter behind a visible "Добавить"
                # control.  A successful application must carry the prepared
                # first-contact text; do not silently fall back to a bare resume.
                add_letter = page.get_by_role("button", name="Сопроводительное письмо Добавить", exact=True)
                if await add_letter.count() == 1:
                    await add_letter.click()
                cover_letter = page.get_by_label("Сопроводительное письмо", exact=True)
                if await cover_letter.count() != 1:
                    screenshot = await self.browser.screenshot(page, "hh-cover-letter-missing")
                    return SubmissionResult(
                        success=False,
                        confirmed=False,
                        detail="HH cover-letter editor was not uniquely available; no blank response was sent.",
                        screenshot_path=screenshot,
                    )
                await cover_letter.fill(prepared_application.body.strip())
                persisted_letter = await cover_letter.evaluate("el => el.value || el.textContent || ''")
                if str(persisted_letter).strip() != prepared_application.body.strip():
                    screenshot = await self.browser.screenshot(page, "hh-cover-letter-unconfirmed")
                    return SubmissionResult(
                        success=False,
                        confirmed=False,
                        detail="HH cover letter did not persist in the visible editor; no response was sent.",
                        screenshot_path=screenshot,
                    )
                submit = page.get_by_role("button", name="Откликнуться", exact=True)
                if await submit.count() != 1:
                    screenshot = await self.browser.screenshot(page, "hh-response-submit-missing")
                    return SubmissionResult(success=False, confirmed=False, detail="HH submit button was not uniquely available.", screenshot_path=screenshot)
                await submit.click()
                await page.wait_for_timeout(700)
                body = await page.locator("body").inner_text()
                confirmation_markers = ("Отклик отправлен", "Ваш отклик отправлен", "Вы откликнулись")
                confirmation = next((item for item in confirmation_markers if item.lower() in body.lower()), None)
                screenshot = await self.browser.screenshot(page, "hh-response-result")
                if confirmation is None:
                    return SubmissionResult(success=False, confirmed=False, detail="HH did not show a confirmed response result.", screenshot_path=screenshot)
                return SubmissionResult(success=True, confirmed=True, external_application_id=prepared_application.external_job_id, detail=confirmation, screenshot_path=screenshot)
            finally:
                await page.close()

    async def check_auth(self) -> AuthStatus:
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            await self.browser.goto(page, "https://hh.ru/applicant/resumes")
            login_link = page.locator('a[href*="account/login"], [data-qa="login"]')
            authenticated = await login_link.count() == 0
            await page.close()
            return AuthStatus(authenticated=authenticated, detail="HH session active" if authenticated else "Login required")

    @staticmethod
    def parse_unread_chats(rows: list[dict[str, str]]) -> list[RawMessage]:
        """Convert only HH rows with the explicit unread badge into messages."""
        messages: list[RawMessage] = []
        for row in rows:
            chat_id = row.get("chat_id", "").strip()
            body = row.get("body", "").strip()
            unread = row.get("unread", "").strip()
            if not chat_id or not body or not unread:
                continue
            message_id = sha256(f"{chat_id}\x00{body}".encode()).hexdigest()
            messages.append(RawMessage(
                external_message_id=message_id,
                site="hh",
                conversation_id=chat_id,
                sender=row.get("sender", "").strip(),
                body=body,
                is_unread=True,
            ))
        return messages

    async def get_unread_messages(self) -> list[RawMessage]:
        """Read HH chat-list badges without opening a conversation or composer."""
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self.browser.goto(page, "https://chatik.hh.ru/?platform=xhh&dest=iframe")
                rows = await page.locator('[data-qa^="chatik-open-chat-"]').evaluate_all("""els => els.map(el => {
                    const href = el.getAttribute('href') || '';
                    const match = href.match(/\\/chat\\/(-?\\d+)/);
                    return {
                        chat_id: match ? match[1] : '',
                        sender: el.querySelector('[data-qa="chat-cell-subtitle"]')?.innerText?.trim() || '',
                        body: el.querySelector('[class^="last-message--"]')?.innerText?.trim() || '',
                        unread: el.querySelector('[data-qa="chatik-info-badges"]')?.innerText?.trim() || ''
                    };
                })""")
                return self.parse_unread_chats(rows)
            finally:
                await page.close()

    async def send_message(self, conversation_id: str, text: str, confirm: bool = False) -> SendMessageResult:
        """Send through HH's internal chat only after explicit confirmation.

        The delivery check requires the newly posted text to appear as a chat
        message, not merely remain in the composer.
        """
        if not confirm:
            return SendMessageResult(success=False, confirmed=False, detail="Explicit confirmation is required before sending an HH chat message.")
        if not re.fullmatch(r"-?\d+", conversation_id):
            return SendMessageResult(success=False, confirmed=False, detail="HH conversation ID is invalid.")
        body = text.strip()
        if not body:
            return SendMessageResult(success=False, confirmed=False, detail="HH chat message cannot be empty.")
        url = f"https://chatik.hh.ru/chat/{conversation_id}?hhtmFrom=app"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self.browser.goto(page, url)
                # HH currently renders a native textarea with a placeholder,
                # not an accessible label.  Its send aria-label contains
                # non-breaking spaces, so an exact text locator is brittle.
                # Cookie consent can also cover the composer on a fresh
                # browser profile; dismiss it before looking for visible UI.
                consent = page.get_by_role("button", name="Понятно", exact=True)
                if await consent.count() == 1 and await consent.is_visible():
                    await consent.click()
                    await page.wait_for_timeout(150)
                composer = await self.selector_recovery.resolve(
                    page,
                    "chat_composer",
                    ['textarea[placeholder="Сообщение"]'],
                    ("сообщ", "message"),
                    'textarea, [contenteditable="true"]',
                )
                send = await self.selector_recovery.resolve(
                    page,
                    "chat_send",
                    ['button[aria-label*="отправить сообщение" i]'],
                    ("отправ", "send"),
                    'button, [role="button"]',
                )
                if composer is None or send is None:
                    screenshot = await self.browser.screenshot(page, "hh-chat-composer-missing")
                    return SendMessageResult(success=False, confirmed=False, detail="HH chat composer was not uniquely available.", screenshot_path=screenshot)
                await composer.fill(body)
                if await composer.evaluate("el => el.value || ''") != body:
                    screenshot = await self.browser.screenshot(page, "hh-chat-text-unconfirmed")
                    return SendMessageResult(success=False, confirmed=False, detail="HH chat text did not persist in the composer.", screenshot_path=screenshot)
                if not await send.is_enabled():
                    screenshot = await self.browser.screenshot(page, "hh-chat-send-disabled")
                    return SendMessageResult(success=False, confirmed=False, detail="HH chat send button did not become enabled.", screenshot_path=screenshot)
                await send.click()
                posted = page.locator("p").filter(has_text=body)
                try:
                    await posted.wait_for(state="visible", timeout=5_000)
                except Exception:
                    screenshot = await self.browser.screenshot(page, "hh-chat-unconfirmed")
                    return SendMessageResult(success=False, confirmed=False, detail="HH did not confirm the message in the conversation.", screenshot_path=screenshot)
                screenshot = await self.browser.screenshot(page, "hh-chat-confirmed")
                return SendMessageResult(success=True, confirmed=True, detail="HH chat shows the sent message.", screenshot_path=screenshot)
            finally:
                await page.close()

    async def search_jobs(self, filters: SearchFilters) -> list[RawJob]:
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                jobs: list[RawJob] = []
                seen: set[str] = set()
                for query_text in filters.queries or [""]:
                    query = quote_plus(query_text)
                    url = f"https://hh.ru/search/vacancy?text={query}&only_with_salary=false&schedule=remote"
                    await self.browser.goto(page, url)
                    cards = page.locator('a[href*="/vacancy/"]')
                    for index in range(await cards.count()):
                        if len(jobs) >= filters.max_results:
                            break
                        link = cards.nth(index)
                        href, title = await link.get_attribute("href"), (await link.inner_text()).strip()
                        match = re.search(r"/vacancy/(\d+)", href or "")
                        if not href or not title or not match or match.group(1) in seen:
                            continue
                        seen.add(match.group(1))
                        jobs.append(RawJob(external_job_id=match.group(1), site=self.site_name, url=href if href.startswith("http") else f"https://hh.ru{href}", title=title, work_format="remote"))
                    if len(jobs) >= filters.max_results:
                        break
                return jobs
            finally:
                await page.close()

    async def get_job_details(self, external_job_id: str) -> RawJobDetails:
        url = f"https://hh.ru/vacancy/{external_job_id}"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page(); await self.browser.goto(page, url)
            title = (await page.locator("h1").inner_text()).strip()
            main = page.locator("main")
            text = (await main.first.inner_text())[:20000] if await main.count() else ""
            return RawJobDetails(external_job_id=external_job_id, site=self.site_name, url=url, title=title, description=text, normalized_text=re.sub(r"\s+", " ", text))

    async def collect_job_details(self, filters: SearchFilters) -> list[RawJobDetails]:
        """Use exactly one persistent profile and one page per scan."""
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                jobs: list[tuple[str, str, str]] = []
                seen: set[str] = set()
                for query_text in filters.queries or [""]:
                    query = quote_plus(query_text)
                    url = f"https://hh.ru/search/vacancy?text={query}&only_with_salary=false&schedule=remote"
                    await self.browser.goto(page, url)
                    cards = page.locator('a[href*="/vacancy/"]')
                    for index in range(await cards.count()):
                        if len(jobs) >= filters.max_results:
                            break
                        link = cards.nth(index)
                        href, title = await link.get_attribute("href"), (await link.inner_text()).strip()
                        match = re.search(r"/vacancy/(\d+)", href or "")
                        if not href or not title or not match or match.group(1) in seen:
                            continue
                        seen.add(match.group(1))
                        jobs.append((match.group(1), href if href.startswith("http") else f"https://hh.ru{href}", title))
                    if len(jobs) >= filters.max_results:
                        break
                details: list[RawJobDetails] = []
                for job_id, job_url, fallback_title in jobs:
                    try:
                        await self.browser.goto(page, job_url)
                        heading = page.locator("h1")
                        title = (await heading.first.inner_text()).strip() if await heading.count() else fallback_title
                        main = page.locator("main")
                        text = (await main.first.inner_text())[:20000] if await main.count() else ""
                        details.append(RawJobDetails(external_job_id=job_id, site=self.site_name, url=job_url, title=title, description=text, normalized_text=re.sub(r"\s+", " ", text)))
                    except Exception:
                        # A removed or unusually structured listing must not
                        # hide the remaining results in a larger scan.
                        continue
                return details
            finally:
                await page.close()

    @staticmethod
    def negotiation_status(text: str) -> str:
        """Map only explicit HH negotiation labels to the portable application states."""
        normalized = " ".join(text.lower().split())
        if "отказ" in normalized:
            return "rejected"
        if "собеседование" in normalized or "приглашение" in normalized:
            return "interview"
        if "не просмотрен" in normalized:
            return "submitted"
        if "просмотрен" in normalized:
            return "viewed"
        return "submitted"

    async def get_application_statuses(self) -> list[ExternalApplicationStatus]:
        """Read existing HH negotiations. This never opens chats or modifies a response."""
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            await self.browser.goto(page, "https://hh.ru/applicant/negotiations")
            cards = page.locator('[data-qa="negotiations-item"]')
            statuses: list[ExternalApplicationStatus] = []
            for index in range(await cards.count()):
                card = cards.nth(index)
                link = card.locator('a[href*="/vacancy/"]').first
                href = await link.get_attribute("href") if await link.count() else None
                match = re.search(r"/vacancy/(\d+)", href or "")
                if not match:
                    continue
                text = (await card.inner_text()).strip()
                statuses.append(ExternalApplicationStatus(
                    external_application_id=match.group(1),
                    status=self.negotiation_status(text),
                    detail=" ".join(text.split())[:1000],
                ))
            await page.close()
            return statuses
