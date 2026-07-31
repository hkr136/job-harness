from __future__ import annotations

import re

from job_agent.browser.manager import BrowserManager
from job_agent.browser.selector_recovery import SelectorRecovery
from job_agent.models import (
    AuthStatus,
    ExternalApplicationStatus,
    PreparedApplication,
    RawJob,
    RawJobDetails,
    SearchFilters,
    SubmissionResult,
)
from job_agent.sites.base import BaseSiteAdapter, SiteCapabilities


class GeekJobAdapter(BaseSiteAdapter):
    site_name = "geekjob"
    capabilities = SiteCapabilities(search_jobs=True, submit_application=True, application_statuses=True)

    def __init__(self, browser: BrowserManager, profile: str = "geekjob") -> None:
        self.browser, self.profile = browser, profile
        self.selector_recovery = SelectorRecovery(self.site_name)

    async def _goto(self, page, url: str) -> None:
        # GeekJob often keeps network activity open; commit is the stable navigation boundary.
        await page.goto(url, wait_until="commit", timeout=30_000)
        await page.wait_for_timeout(2_000)

    async def check_auth(self) -> AuthStatus:
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page(); await self._goto(page, "https://my.geekjob.ru/")
            authenticated = await page.get_by_text("Выход", exact=True).count() > 0
            await page.close()
            return AuthStatus(authenticated=authenticated, detail="GeekJob account dashboard" if authenticated else "Login required")

    async def search_jobs(self, filters: SearchFilters) -> list[RawJob]:
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page(); await self._goto(page, "https://geekjob.ru/vacancies")
            # A GeekJob card contains several links to the same vacancy:
            # company, salary and date. Only ``a.title`` is the vacancy title;
            # choosing the longest arbitrary link text used to store a company
            # or compensation line as the role.
            links = await page.locator('a.title[href*="/vacancy/"]').evaluate_all("els=>els.map(a=>({href:a.getAttribute('href'),text:a.innerText.trim()}))")
            titles: dict[str, str] = {}
            for link in links:
                match = re.search(r"/vacancy/([a-z0-9]+)", link["href"] or "", re.I)
                if match and link["text"].strip():
                    titles[match.group(1)] = link["text"]
            await page.close()
            return [RawJob(external_job_id=job_id, site=self.site_name, url=f"https://geekjob.ru/vacancy/{job_id}", title=title, work_format="remote") for job_id, title in list(titles.items())[:filters.max_results]]

    async def get_job_details(self, external_job_id: str) -> RawJobDetails:
        url = f"https://geekjob.ru/vacancy/{external_job_id}"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page(); await self._goto(page, url)
            heading = page.locator("h1")
            title = (await heading.inner_text()).strip() if await heading.count() else external_job_id
            text = (await page.locator("body").inner_text())[:20000]
            await page.close()
            return RawJobDetails(external_job_id=external_job_id, site=self.site_name, url=url, title=title, description=text, normalized_text=re.sub(r"\s+", " ", text))

    @staticmethod
    def response_status(text: str) -> str:
        """Map only explicit GeekJob response labels to portable funnel states."""
        normalized = " ".join(text.lower().split())
        if "отказ" in normalized:
            return "rejected"
        if "собеседован" in normalized or "интервью" in normalized:
            return "interview"
        if "прочитано" in normalized:
            return "viewed"
        return "submitted"

    async def get_application_statuses(self) -> list[ExternalApplicationStatus]:
        """Read the authenticated response list without opening employer chats."""
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            try:
                await self._goto(page, "https://my.geekjob.ru/responses")
                links = page.locator('a[href*="/vacancy/"]')
                statuses: list[ExternalApplicationStatus] = []
                seen: set[str] = set()
                for index in range(await links.count()):
                    link = links.nth(index)
                    href = await link.get_attribute("href")
                    match = re.search(r"/vacancy/([a-z0-9]+)", href or "", re.I)
                    if not match or match.group(1) in seen:
                        continue
                    seen.add(match.group(1))
                    text = await link.evaluate(
                        "el => (el.closest('li') || el.parentElement?.parentElement)?.innerText || el.innerText"
                    )
                    statuses.append(ExternalApplicationStatus(
                        external_application_id=match.group(1),
                        status=self.response_status(str(text)),
                        detail=" ".join(str(text).split())[:1000],
                    ))
                return statuses
            finally:
                await page.close()

    async def submit_application(self, prepared_application: PreparedApplication, confirm: bool = False) -> SubmissionResult:
        """Submit through GeekJob's native response form and require its success banner."""
        if not confirm:
            return SubmissionResult(success=False, confirmed=False, detail="Dry run only. Repeat with explicit confirmation.")
        if not prepared_application.external_job_id:
            return SubmissionResult(success=False, confirmed=False, detail="GeekJob vacancy ID is required.")
        url = f"https://geekjob.ru/vacancy/{prepared_application.external_job_id}"
        async with self.browser.context(self.profile) as ctx:
            page = await ctx.new_page()
            await self._goto(page, url)
            page_text = await page.locator("body").inner_text()
            if "Уже откликнулись" in page_text or "Вы успешно отправили запрос" in page_text:
                await page.close()
                return SubmissionResult(success=False, confirmed=False, detail="The site already shows an existing response; duplicate was not sent.")
            letter = page.get_by_label("Измените сопроводительный текст по своему усмотрению", exact=True)
            submit = page.get_by_role("button", name="Откликнуться на вакансию", exact=True)
            if await letter.count() != 1:
                letter = await self.selector_recovery.resolve(
                    page, "application_letter", ["textarea"], ("сопровод", "текст"), 'textarea, [contenteditable="true"]'
                )
            if await submit.count() != 1:
                submit = await self.selector_recovery.resolve(
                    page, "application_submit", [], ("отклик", "apply"), 'button, [role="button"]'
                )
            if letter is None or submit is None or await letter.count() != 1 or await submit.count() != 1:
                screenshot = await self.browser.screenshot(page, "geekjob-response-form-missing")
                await page.close()
                return SubmissionResult(success=False, confirmed=False, detail="GeekJob response form was not found.", screenshot_path=screenshot)
            await letter.fill(prepared_application.body)
            await submit.click()
            await page.wait_for_timeout(500)
            page_text = await page.locator("body").inner_text()
            confirmation = "Вы успешно отправили запрос на эту вакансию"
            if confirmation not in page_text:
                screenshot = await self.browser.screenshot(page, "geekjob-unconfirmed-response")
                await page.close()
                return SubmissionResult(success=False, confirmed=False, detail="GeekJob did not confirm the response.", screenshot_path=screenshot)
            screenshot = await self.browser.screenshot(page, "geekjob-response-confirmed")
            await page.close()
            return SubmissionResult(success=True, confirmed=True, external_application_id=prepared_application.external_job_id, detail="GeekJob confirmed the response.", screenshot_path=screenshot)
