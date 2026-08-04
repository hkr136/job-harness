from __future__ import annotations

import re
from hashlib import sha256

from job_agent.browser.manager import BrowserManager
from job_agent.browser.selector_recovery import SelectorRecovery
from job_agent.models import (
    AuthStatus,
    ExternalApplicationStatus,
    PreparedApplication,
    RawJob,
    RawJobDetails,
    RawMessage,
    SearchFilters,
    SendMessageResult,
    SubmissionResult,
)
from job_agent.sites.base import BaseSiteAdapter, SiteCapabilities


class HabrAdapter(BaseSiteAdapter):
    site_name = "habr"
    capabilities = SiteCapabilities(
        search_jobs=True,
        submit_application=True,
        read_messages=True,
        send_messages=True,
        application_statuses=True,
    )

    def __init__(self, browser: BrowserManager, profile: str = "habr") -> None:
        self.browser, self.profile = browser, profile
        self.selector_recovery = SelectorRecovery(self.site_name)

    async def check_auth(self) -> AuthStatus:
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            await self.browser.goto(page, "https://career.habr.com/vacancies?type=suitable")
            authenticated = "type=suitable" in page.url and await page.get_by_text("Подходящие", exact=True).count() > 0
            await page.close()
            return AuthStatus(authenticated=authenticated, detail="Habr Career suitable vacancies" if authenticated else "Login required")

    async def search_jobs(self, filters: SearchFilters) -> list[RawJob]:
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            await self.browser.goto(page, "https://career.habr.com/vacancies?type=suitable")
            links = await page.locator('a[href^="/vacancies/"]').evaluate_all("els => els.map(a => ({href:a.getAttribute('href'), text:a.innerText.trim()}))")
            seen: set[str] = set(); jobs: list[RawJob] = []
            for link in links:
                match = re.fullmatch(r"/vacancies/(\d+)", link["href"] or "")
                if not match or not link["text"] or match.group(1) in seen:
                    continue
                seen.add(match.group(1))
                jobs.append(RawJob(external_job_id=match.group(1), site=self.site_name, url=f"https://career.habr.com{link['href']}", title=link["text"], work_format="remote"))
                if len(jobs) >= filters.max_results:
                    break
            await page.close()
            return jobs

    async def get_job_details(self, external_job_id: str) -> RawJobDetails:
        url = f"https://career.habr.com/vacancies/{external_job_id}"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page(); await self.browser.goto(page, url)
            title = (await page.locator("h1").inner_text()).strip()
            main = page.locator("main")
            text = (await main.first.inner_text())[:20000] if await main.count() else ""
            await page.close()
            return RawJobDetails(external_job_id=external_job_id, site=self.site_name, url=url, title=title, description=text, normalized_text=re.sub(r"\s+", " ", text))

    async def collect_job_details(self, filters: SearchFilters) -> list[RawJobDetails]:
        """Reuse one Habr page for a batch instead of relaunching per vacancy."""
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self.browser.goto(page, "https://career.habr.com/vacancies?type=suitable")
                links = await page.locator('a[href^="/vacancies/"]').evaluate_all(
                    "els => els.map(a => ({href:a.getAttribute('href'), text:a.innerText.trim()}))"
                )
                jobs = self._jobs_from_links(links, filters.max_results)
                details: list[RawJobDetails] = []
                for job in jobs:
                    await self.browser.goto(page, job.url)
                    heading = page.locator("h1")
                    title = (await heading.inner_text()).strip() if await heading.count() else job.title
                    main = page.locator("main")
                    text = (await main.inner_text())[:20_000] if await main.count() else ""
                    details.append(RawJobDetails.model_validate({
                        **job.model_dump(), "title": title, "description": text,
                        "normalized_text": re.sub(r"\s+", " ", text),
                    }))
                return details
            finally:
                await page.close()

    def _jobs_from_links(self, links: list[dict[str, str | None]], max_results: int) -> list[RawJob]:
        seen: set[str] = set()
        jobs: list[RawJob] = []
        for link in links:
            href, title = str(link.get("href") or ""), str(link.get("text") or "").strip()
            match = re.fullmatch(r"/vacancies/(\d+)", href)
            if not match or not title or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            jobs.append(RawJob(
                external_job_id=match.group(1), site=self.site_name,
                url=f"https://career.habr.com{href}", title=title, work_format="remote",
            ))
            if len(jobs) >= max_results:
                break
        return jobs

    @staticmethod
    def parse_conversation_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        """Keep only conversations whose latest preview is inbound.

        Habr does not expose a durable per-thread unread attribute in its
        conversation list.  The last preview is its stable signal: a ``Вы:``
        prefix means the latest entry is the candidate's own message.
        """
        return [
            row for row in rows
            if row.get("conversation_id") and row.get("preview", "").strip()
            and not row["preview"].lstrip().startswith("Вы:")
        ]

    async def get_unread_messages(self) -> list[RawMessage]:
        """Read the latest inbound message from each actionable Habr dialog."""
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self.browser.goto(page, "https://career.habr.com/conversations")
                rows = await page.locator('a[href^="/conversations/"]').evaluate_all("""els => els.map(link => {
                    const lines = (link.innerText || '').split('\\n').map(v => v.trim()).filter(Boolean);
                    const href = link.getAttribute('href') || '';
                    return {
                        conversation_id: href.replace(/^\\/conversations\\//, ''),
                        sender: link.querySelector('strong')?.innerText?.trim() || lines[0] || '',
                        preview: lines.at(-1) || ''
                    };
                })""")
                messages: list[RawMessage] = []
                for row in self.parse_conversation_rows(rows):
                    conversation_id = row["conversation_id"]
                    await self.browser.goto(page, f"https://career.habr.com/conversations/{conversation_id}")
                    latest = await page.locator(".conversation-messages").evaluate("""root => {
                        const messages = [...root.querySelectorAll('[data-message-id]')]
                            .filter(node => node.querySelector('.message-body'));
                        const node = messages.at(-1);
                        if (!node) return null;
                        return {
                            id: node.getAttribute('data-message-id') || '',
                            body: node.querySelector('.message-body')?.innerText?.trim() || ''
                        };
                    }""")
                    if not isinstance(latest, dict) or not latest.get("id") or not latest.get("body"):
                        continue
                    messages.append(RawMessage(
                        external_message_id=sha256(f"{conversation_id}\x00{latest['id']}".encode()).hexdigest(),
                        site=self.site_name,
                        conversation_id=conversation_id,
                        sender=row.get("sender", ""),
                        body=str(latest["body"]),
                        is_unread=True,
                    ))
                return messages
            finally:
                await page.close()

    async def send_message(self, conversation_id: str, text: str, confirm: bool = False) -> SendMessageResult:
        """Send only via a Habr Career dialog and prove it appeared there."""
        if not confirm:
            return SendMessageResult(success=False, confirmed=False, detail="Explicit confirmation is required before sending a Habr Career message.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", conversation_id):
            return SendMessageResult(success=False, confirmed=False, detail="Habr conversation ID is invalid.")
        body = text.strip()
        if not body:
            return SendMessageResult(success=False, confirmed=False, detail="Habr Career message cannot be empty.")
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self.browser.goto(page, f"https://career.habr.com/conversations/{conversation_id}")
                composer = await self.selector_recovery.resolve(
                    page, "chat_composer", ['textarea[placeholder="Сообщение..."]'],
                    ("сообщ", "message"), 'textarea, [contenteditable="true"]',
                )
                send = page.locator('form[action="/"] button[type="submit"]')
                if composer is None or await send.count() != 1:
                    screenshot = await self.browser.screenshot(page, "habr-chat-composer-missing")
                    return SendMessageResult(success=False, confirmed=False, detail="Habr Career chat composer was not uniquely available.", screenshot_path=screenshot)
                await composer.fill(body)
                if (await composer.evaluate("el => el.value || el.textContent || ''")).strip() != body:
                    screenshot = await self.browser.screenshot(page, "habr-chat-text-unconfirmed")
                    return SendMessageResult(success=False, confirmed=False, detail="Habr Career chat text did not persist in the composer.", screenshot_path=screenshot)
                if not await send.is_enabled():
                    screenshot = await self.browser.screenshot(page, "habr-chat-send-disabled")
                    return SendMessageResult(success=False, confirmed=False, detail="Habr Career chat send button did not become enabled.", screenshot_path=screenshot)
                posted = page.locator(".conversation-messages .message-body").filter(has_text=body)
                before = await posted.count()
                await send.click()
                for _ in range(10):
                    await page.wait_for_timeout(500)
                    if await posted.count() > before:
                        break
                else:
                    screenshot = await self.browser.screenshot(page, "habr-chat-unconfirmed")
                    return SendMessageResult(success=False, confirmed=False, detail="Habr Career did not confirm the message in the conversation.", screenshot_path=screenshot)
                screenshot = await self.browser.screenshot(page, "habr-chat-confirmed")
                return SendMessageResult(success=True, confirmed=True, detail="Habr Career chat shows the sent message.", screenshot_path=screenshot)
            finally:
                await page.close()

    @staticmethod
    def response_status(text: str) -> str:
        normalized = " ".join(text.lower().split())
        if "не прочитано" in normalized:
            return "submitted"
        if "прочитано" in normalized:
            return "viewed"
        return "submitted"

    async def get_application_statuses(self) -> list[ExternalApplicationStatus]:
        """Read the Habr Career response list without visiting an employer conversation."""
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            await self.browser.goto(page, "https://career.habr.com/responses")
            links = page.locator('a[href^="/vacancies/"]')
            statuses: list[ExternalApplicationStatus] = []
            seen: set[str] = set()
            for index in range(await links.count()):
                link = links.nth(index)
                href = await link.get_attribute("href")
                match = re.fullmatch(r"/vacancies/(\d+)", href or "")
                if not match or match.group(1) in seen:
                    continue
                seen.add(match.group(1))
                text = await link.evaluate("el => (el.closest('tr') || el.parentElement?.parentElement)?.innerText || el.innerText")
                statuses.append(ExternalApplicationStatus(
                    external_application_id=match.group(1),
                    status=self.response_status(text),
                    detail=" ".join(text.split())[:1000],
                ))
            await page.close()
            return statuses

    async def submit_application(self, prepared_application: PreparedApplication, confirm: bool = False) -> SubmissionResult:
        """Confirmed Habr Career flow: submit response, then append the cover letter if offered."""
        if not confirm:
            return SubmissionResult(success=False, confirmed=False, detail="Dry run only. Repeat with explicit confirmation.")
        if not prepared_application.external_job_id:
            return SubmissionResult(success=False, confirmed=False, detail="Habr vacancy ID is required.")
        url = f"https://career.habr.com/vacancies/{prepared_application.external_job_id}"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            await self.browser.goto(page, url)
            page_text = await page.locator("body").inner_text()
            if "Посмотреть отклик" in page_text or "Вы откликнулись" in page_text:
                await page.close()
                return SubmissionResult(success=False, confirmed=False, detail="The site already shows an existing response; duplicate was not sent.")
            response_button = page.get_by_role("button", name="Откликнуться", exact=True).nth(0)
            if await response_button.count() == 0:
                response_button = await self.selector_recovery.resolve(
                    page, "application_submit", [], ("отклик", "apply"), 'button, [role="button"]'
                )
            if response_button is None or await response_button.count() == 0:
                await page.close()
                return SubmissionResult(success=False, confirmed=False, detail="Habr response button was not found.")
            await response_button.click()
            await page.wait_for_timeout(500)
            page_text = await page.locator("body").inner_text()
            if "Отклик отправлен" not in page_text and "Посмотреть отклик" not in page_text:
                screenshot = await self.browser.screenshot(page, "habr-unconfirmed-response")
                await page.close()
                return SubmissionResult(success=False, confirmed=False, detail="Habr did not confirm the response.", screenshot_path=screenshot)
            letter = page.locator('textarea[name="body"]')
            append = page.get_by_role("button", name="Дополнить отклик", exact=True)
            if prepared_application.body.strip() and await letter.count() and await append.count():
                await letter.fill(prepared_application.body)
                await append.click()
                await page.wait_for_timeout(500)
                page_text = await page.locator("body").inner_text()
                if prepared_application.body.strip() not in page_text:
                    screenshot = await self.browser.screenshot(page, "habr-unconfirmed-letter")
                    await page.close()
                    return SubmissionResult(success=False, confirmed=False, detail="Habr did not confirm the cover letter.", screenshot_path=screenshot)
            screenshot = await self.browser.screenshot(page, "habr-response-confirmed")
            await page.close()
            return SubmissionResult(success=True, confirmed=True, external_application_id=prepared_application.external_job_id, detail="Habr Career confirmed the response.", screenshot_path=screenshot)
