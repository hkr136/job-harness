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


class HabrAdapter(BaseSiteAdapter):
    site_name = "habr"
    capabilities = SiteCapabilities(search_jobs=True, submit_application=True, application_statuses=True)

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
