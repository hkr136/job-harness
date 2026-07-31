from __future__ import annotations

import re
from hashlib import sha256
from typing import Any

from playwright.async_api import Locator

from job_agent.browser.manager import BrowserManager
from job_agent.browser.selector_recovery import SelectorRecovery
from job_agent.models import (
    AuthStatus,
    PreparedApplication,
    RawJob,
    RawJobDetails,
    RawMessage,
    SearchFilters,
    SubmissionResult,
)
from job_agent.sites.base import BaseSiteAdapter, SiteCapabilities


class KworkAdapter(BaseSiteAdapter):
    site_name = "kwork"
    capabilities = SiteCapabilities(search_jobs=True, submit_application=True, read_messages=True)
    PROJECTS_URL = "https://kwork.ru/projects"
    _PROJECT_HREF = re.compile(r"(?:https?://kwork\.ru)?/projects/(\d+)(?:/view)?/?$")

    @staticmethod
    def extract_project_description(page_text: str, title: str = "") -> str:
        """Keep project content, not Kwork navigation/footer chrome."""
        text = page_text.replace("\xa0", " ")
        if "К списку проектов" in text:
            text = text.split("К списку проектов", 1)[1]
        if "О Kwork" in text:
            text = text.split("О Kwork", 1)[0]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if title and lines and lines[0] == title:
            lines = lines[1:]
        return "\n".join(lines).strip()

    def __init__(self, browser: BrowserManager, profile: str = "kwork") -> None:
        self.browser, self.profile = browser, profile
        self.selector_recovery = SelectorRecovery(self.site_name)

    async def _goto_feed_page(self, page: Any, page_number: int = 1) -> None:
        """Navigate Kwork's Vue feed without waiting for its long-lived requests."""
        suffix = "" if page_number == 1 else f"?page={page_number}"
        await page.goto(f"{self.PROJECTS_URL}{suffix}", wait_until="commit", timeout=30_000)
        # Keep a human-scale pause while using ``commit`` as the navigation
        # boundary; ``domcontentloaded`` can wait indefinitely on this feed.
        await page.wait_for_timeout(2_500)

    async def check_auth(self) -> AuthStatus:
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page(); await self.browser.goto(page, "https://kwork.ru/seller")
            authenticated = await page.get_by_text("Продавец", exact=True).count() > 0
            await page.close()
            return AuthStatus(authenticated=authenticated, detail="Kwork seller dashboard" if authenticated else "Login required")

    async def search_jobs(self, filters: SearchFilters) -> list[RawJob]:
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self._goto_feed_page(page)
                return await self._read_project_pages(page, filters)
            finally:
                await page.close()

    async def _read_project_pages(self, page: Any, filters: SearchFilters) -> list[RawJob]:
        """Collect each visible Kwork feed page, not just page one.

        The redesigned feed uses clickable ``div`` pagination rather than
        links. Its stable URL query is safer for unattended reading and keeps
        the result bounded by the configured global maximum.
        """
        page_numbers = await page.locator(".pagination__item").evaluate_all(
            "els => els.map(el => Number((el.innerText || '').trim())).filter(Number.isFinite)"
        )
        last_page = max([int(value) for value in page_numbers] or [1])
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for page_number in range(1, last_page + 1):
            if page_number > 1:
                try:
                    await self._goto_feed_page(page, page_number)
                except Exception:
                    # Keep the rest of the feed usable if a single dynamic
                    # page times out; its trace/screenshot is retained by the
                    # browser context for recovery.
                    continue
            for job in await self._read_project_feed(page, filters):
                if job.external_job_id in seen:
                    continue
                seen.add(job.external_job_id)
                jobs.append(job)
                if len(jobs) >= filters.max_results:
                    return jobs
        return jobs

    @classmethod
    def parse_project_links(cls, links: list[dict[str, str | None]], max_results: int) -> list[RawJob]:
        """Extract only project cards, never seller-dashboard offer buttons.

        Kwork's project feed uses `/projects/<id>` links; opening one may
        redirect to `/view`, so both URL forms are accepted. Links to a
        customer's project list are deliberately excluded by the exact shape.
        """
        seen: set[str] = set()
        jobs: list[RawJob] = []
        for link in links:
            href, title = str(link.get("href") or ""), str(link.get("text") or "").strip()
            match = cls._PROJECT_HREF.fullmatch(href)
            if not match or not title or match.group(1) in seen:
                continue
            project_id = match.group(1)
            seen.add(project_id)
            jobs.append(
                RawJob(
                    external_job_id=project_id,
                    site=cls.site_name,
                    url=f"https://kwork.ru/projects/{project_id}/view",
                    title=title,
                    work_format="remote",
                )
            )
            if len(jobs) >= max_results:
                break
        return jobs

    async def _read_project_feed(self, page: Any, filters: SearchFilters | dict[str, Any]) -> list[RawJob]:
        project_links = page.locator('a[href^="/projects/"]')
        try:
            await project_links.first.wait_for(state="attached", timeout=10_000)
        except Exception:
            # An empty feed is possible, but an unknown markup change must not
            # be reported as "there are no orders".
            pass
        links = await project_links.evaluate_all(
            "els => els.map(a => ({href: a.getAttribute('href'), text: (a.innerText || '').trim()}))"
        )
        max_results = filters.max_results if isinstance(filters, SearchFilters) else int(filters.get("max_results", 8))
        jobs = self.parse_project_links(links, max_results)
        if jobs:
            return jobs
        body = await page.locator("body").inner_text()
        if "Биржа проектов" in body and "Нет проектов" not in body:
            screenshot = await self.browser.screenshot(page, "kwork-project-feed-empty")
            raise RuntimeError(f"Kwork project feed contained no recognized project cards; artifact: {screenshot}")
        return []

    async def get_job_details(self, external_job_id: str) -> RawJobDetails:
        url = f"https://kwork.ru/projects/{external_job_id}/view"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page(); await self.browser.goto(page, url)
            title_locator = page.locator("h1")
            title = (await title_locator.inner_text()).strip() if await title_locator.count() else f"Kwork project {external_job_id}"
            text = self.extract_project_description((await page.locator("body").inner_text())[:20000], title)
            await page.close()
            return RawJobDetails(external_job_id=external_job_id, site=self.site_name, url=url, title=title, description=text, normalized_text=re.sub(r"\s+", " ", text))

    async def collect_job_details(self, filters: SearchFilters) -> list[RawJobDetails]:
        """Read the seller feed in one persistent context, avoiding window churn."""
        details: list[RawJobDetails] = []
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self._goto_feed_page(page)
                jobs = await self._read_project_pages(page, filters)
                for job in jobs:
                    try:
                        await self.browser.goto(page, job.url)
                        heading = page.locator("h1")
                        title = (await heading.first.inner_text()).strip() if await heading.count() else job.title
                        text = self.extract_project_description((await page.locator("body").inner_text())[:20000], title)
                        details.append(RawJobDetails(external_job_id=job.external_job_id, site=self.site_name, url=job.url, title=title, description=text, normalized_text=re.sub(r"\s+", " ", text)))
                    except Exception:
                        continue
            finally:
                await page.close()
        return details

    @staticmethod
    def validate_offer(prepared: PreparedApplication) -> str | None:
        if not prepared.external_job_id or not prepared.title or not prepared.price or not prepared.duration:
            return "Kwork offer requires project ID, title, price and duration"
        body = prepared.body.strip()
        if not 150 <= len(body) <= 2000:
            return "Kwork description must contain 150–2000 characters"
        if len(prepared.title.strip()) > 100:
            return "Kwork offer title is too long"
        if not re.fullmatch(r"[\d\s]+", prepared.price):
            return "Kwork price must contain digits only"
        # Keep Kwork communication on-platform. This checks contact-shaped data,
        # not ordinary domain words which may belong to the project itself.
        contact = r"(?:https?://|www\.|t\.me/|@[a-zA-Z0-9_]{4,}|[\w.+-]+@[\w-]+\.[\w.-]+|\+?\d[\d\s()\-]{8,})"
        if re.search(contact, body):
            return "Kwork offer contains an external contact; keep communication inside Kwork"
        return None

    async def _set_duration(self, page: Any, days: str) -> bool:
        """Set Kwork's Vue duration select without assuming an old text input.

        The current form exposes a ``v-select`` component with a read-only
        displayed value plus a search input, not the former
        ``placeholder='Срок выполнения'`` input. It often already contains a
        valid default; keep it when it matches rather than opening a menu.
        """
        selector = page.locator(".duration-select")
        if await selector.count() != 1 or not await selector.is_visible():
            selector = await self.selector_recovery.resolve(
                page, "offer_duration", [".duration-select"], ("срок", "день"), '[role="combobox"], input, button'
            )
        if selector is None or await selector.count() != 1:
            return False
        selected = selector.locator(".duration-select__selected-option")
        if await selected.count() == 1:
            try:
                if re.search(rf"\b{re.escape(days)}\b", await selected.input_value()):
                    return True
            except Exception:
                pass
        toggle = selector.locator('[role="combobox"]')
        if await toggle.count() != 1:
            toggle = selector
        await toggle.click()
        options = page.locator('[role="option"]')
        for index in range(await options.count()):
            option = options.nth(index)
            try:
                text = (await option.inner_text()).strip()
                if await option.is_visible() and re.search(rf"\b{re.escape(days)}\b", text):
                    await option.click()
                    return True
            except Exception:
                continue
        # Vue Select accepts filtering and Enter on its search input. This is
        # a bounded fallback, not a blind click on arbitrary page text.
        search = selector.locator("input.vs__search")
        if await search.count() == 1:
            await search.fill(days)
            await search.press("Enter")
            if await selected.count() == 1:
                try:
                    return bool(re.search(rf"\b{re.escape(days)}\b", await selected.input_value()))
                except Exception:
                    return False
        return False

    async def submit_application(self, prepared_application: PreparedApplication, confirm: bool = False) -> SubmissionResult:
        """Submit only when the caller explicitly confirms and Kwork confirms success."""
        error = self.validate_offer(prepared_application)
        if error:
            return SubmissionResult(success=False, confirmed=False, detail=error)
        if not confirm:
            return SubmissionResult(success=False, confirmed=False, detail="Explicit confirmation is required before submitting a Kwork offer")
        project_url = f"https://kwork.ru/projects/{prepared_application.external_job_id}/view"
        offer_url = f"https://kwork.ru/new_offer?project={prepared_application.external_job_id}"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                # A direct /new_offer navigation can be redirected by Kwork
                # back to the project feed. Open the project's own offer link
                # first: it preserves the current authenticated/referrer flow.
                await self.browser.goto(page, project_url)
                project_state = await page.evaluate(
                    """() => {
                        const want = window.stateData && window.stateData.wantData;
                        return want ? {status: want.status, active: want.isWantActive} : null;
                    }"""
                )
                if isinstance(project_state, dict) and project_state.get("active") is False:
                    screenshot = await self.browser.screenshot(page, "kwork-project-inactive")
                    status = str(project_state.get("status") or "closed")
                    return SubmissionResult(
                        success=False,
                        confirmed=False,
                        detail=f"Kwork project is no longer active (status: {status}); no offer was sent",
                        screenshot_path=screenshot,
                    )
                offer_link = page.locator(f'a[href*="new_offer"][href*="project={prepared_application.external_job_id}"]')
                if await offer_link.count() == 1 and await offer_link.is_visible():
                    await offer_link.click()
                    try:
                        await page.wait_for_url(re.compile(r".*/new_offer\?project="), timeout=12_000)
                    except Exception:
                        # The next editor check provides a concrete diagnostic
                        # if Kwork kept us on the project or redirected away.
                        pass
                else:
                    await self.browser.goto(page, offer_url)
                description = page.locator('div.trumbowyg-editor[placeholder="Напишите, как вы будете решать задачу клиента"]')
                title = page.locator('div.trumbowyg-editor[placeholder="Введите название заказа"]')
                if await description.count() != 1:
                    description = await self.selector_recovery.resolve(
                        page, "offer_description", ['div[contenteditable="true"]'], ("решать", "задач"), 'div[contenteditable="true"], textarea'
                    )
                if await title.count() != 1:
                    title = await self.selector_recovery.resolve(
                        page, "offer_title", ['div[contenteditable="true"]'], ("название", "заказ"), 'div[contenteditable="true"], textarea'
                    )
                if description is None or title is None or await description.count() != 1 or await title.count() != 1:
                    screenshot = await self.browser.screenshot(page, "kwork-offer-editors-missing")
                    return SubmissionResult(
                        success=False,
                        confirmed=False,
                        detail=f"Kwork offer form did not open (landed at {page.url}).",
                        screenshot_path=screenshot,
                    )
                if not await self._write_verified_rich_text(description, prepared_application.body):
                    return SubmissionResult(success=False, confirmed=False, detail="Kwork description did not persist in the visible rich-text field")
                if not await self._write_verified_rich_text(title, prepared_application.title):
                    return SubmissionResult(success=False, confirmed=False, detail="Kwork title did not persist in the visible rich-text field")
                price = page.locator("#offer-custom-price")
                if await price.count() != 1 or not await price.is_visible():
                    price = await self.selector_recovery.resolve(
                        page, "offer_price", ["#offer-custom-price"], ("стоим", "цен"), 'input[type="tel"], input[type="number"], input[type="text"]'
                    )
                if price is None or await price.count() != 1:
                    screenshot = await self.browser.screenshot(page, "kwork-offer-price-missing")
                    return SubmissionResult(success=False, confirmed=False, detail="Kwork offer price field was not uniquely available.", screenshot_path=screenshot)
                await price.fill(re.sub(r"\s+", "", prepared_application.price))
                if not await self._set_duration(page, prepared_application.duration):
                    screenshot = await self.browser.screenshot(page, "kwork-offer-duration-missing")
                    return SubmissionResult(success=False, confirmed=False, detail="Kwork duration selector could not be set to the proposed deadline.", screenshot_path=screenshot)
                submit = page.get_by_role("button", name="Предложить", exact=True)
                if await submit.count() != 1:
                    submit = await self.selector_recovery.resolve(
                        page, "offer_submit", [], ("предлож", "offer"), 'button, [role="button"]'
                    )
                if submit is None or await submit.count() != 1:
                    screenshot = await self.browser.screenshot(page, "kwork-offer-submit-missing")
                    return SubmissionResult(success=False, confirmed=False, detail="Kwork offer submit button was not uniquely available.", screenshot_path=screenshot)
                if not await submit.is_enabled():
                    screenshot = await self.browser.screenshot(page, "kwork-offer-submit-disabled")
                    return SubmissionResult(success=False, confirmed=False, detail="Kwork offer submit button is disabled; a required field is still invalid.", screenshot_path=screenshot)
                await submit.click()
                confirmation = "Ваше индивидуальное предложение отправлено"
                try:
                    await page.get_by_text(confirmation, exact=False).first.wait_for(state="visible", timeout=20_000)
                except Exception:
                    screenshot = str((await self.browser.screenshot(page, "kwork-offer-result")))
                    return SubmissionResult(
                        success=False,
                        confirmed=False,
                        detail="Kwork did not show the required offer confirmation within 20 seconds",
                        screenshot_path=screenshot,
                    )
                screenshot = str((await self.browser.screenshot(page, "kwork-offer-result")))
                return SubmissionResult(success=True, confirmed=True, detail=confirmation, screenshot_path=screenshot)
            finally:
                await page.close()

    @staticmethod
    def _normalise_editor_text(value: str) -> str:
        return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()

    async def _write_verified_rich_text(self, editor: Locator, value: str) -> bool:
        """Write through the visible Trumbowyg editor and prove rendered text.

        Kwork occasionally accepts ``fill`` without updating its own editor
        model. Keyboard input plus native input/change events follows the same
        path as a seller typing manually; verification compares normalized
        visible text rather than fragile string length.
        """
        expected = self._normalise_editor_text(value)
        try:
            await editor.click()
            try:
                await editor.press("Meta+A")
            except Exception:
                await editor.press("Control+A")
            await editor.press_sequentially(value, delay=1)
            await editor.evaluate("""element => {
                element.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText'}));
                element.dispatchEvent(new Event('change', {bubbles: true}));
                element.dispatchEvent(new Event('blur', {bubbles: true}));
            }""")
            actual = self._normalise_editor_text(await editor.inner_text())
            return actual == expected
        except Exception:
            return False

    @staticmethod
    def parse_unread_rows(rows: list[dict[str, Any]]) -> list[RawMessage]:
        """Turn only rows explicitly marked unread by Kwork into messages.

        A conversation preview alone is not evidence of a new employer message: it
        may be years old or be the seller's own last reply.  This deliberately
        conservative parser waits for Kwork's unread/new UI marker, so the
        automation cannot generate a false "new message" alert.
        """
        messages: list[RawMessage] = []
        for row in rows:
            classes = " ".join(row.get("classes", [])).lower()
            if "unread" not in classes and "new" not in classes:
                continue
            sender = str(row.get("sender", "")).strip()
            body = str(row.get("body", "")).strip()
            if not sender or not body or body.startswith("Вы:"):
                continue
            conversation_id = str(row.get("conversation_id") or sender)
            message_id = sha256(f"{conversation_id}\x00{body}".encode()).hexdigest()
            messages.append(
                RawMessage(
                    external_message_id=message_id,
                    site="kwork",
                    conversation_id=conversation_id,
                    sender=sender,
                    body=body,
                    is_unread=True,
                )
            )
        return messages

    async def get_unread_messages(self) -> list[RawMessage]:
        """Read the inbox without opening a conversation or touching its composer."""
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            await self.browser.goto(page, "https://kwork.ru/inbox")
            rows = await page.locator("li.chat__list-item").evaluate_all(
                """els => els.map((item, index) => ({
                    classes: [...item.querySelectorAll('*')].map(node => typeof node.className === 'string' ? node.className : (node.getAttribute('class') || '')),
                    conversation_id: item.getAttribute('data-chat-id') || item.getAttribute('data-conversation-id') || String(index),
                    sender: item.querySelector('.chat__list-user')?.innerText?.trim() || '',
                    body: item.querySelector('.chat__list-message')?.innerText?.trim() || ''
                }))"""
            )
            await page.close()
            return self.parse_unread_rows(rows)
