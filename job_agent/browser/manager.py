from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from playwright.async_api import BrowserContext, Page, async_playwright

from job_agent.config.settings import USER_HOME, ensure_user_home


class BrowserManager:
    """Persistent, isolated automation profiles. Never uses the main Chrome profile."""

    def __init__(
        self,
        headless: bool = True,
        min_action_delay_seconds: float = 2.5,
        max_action_delay_seconds: float = 5.0,
    ) -> None:
        self.headless = headless
        self.min_action_delay_seconds = min_action_delay_seconds
        self.max_action_delay_seconds = max(max_action_delay_seconds, min_action_delay_seconds)

    @asynccontextmanager
    async def context(self, profile: str):
        user_data_dir = ensure_user_home() / "browser-profiles" / profile
        user_data_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            context: BrowserContext = await pw.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=self.headless,
            )
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            try:
                yield context
            except Exception:
                trace_dir = ensure_user_home() / "artifacts" / "playwright-traces"
                trace_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                await context.tracing.stop(path=str(trace_dir / f"{profile}-{stamp}.zip"))
                raise
            finally:
                # `stop` is idempotent only once; a failure path already wrote its trace.
                try:
                    await context.tracing.stop()
                except Exception:
                    pass
                await context.close()

    async def goto(self, page: Page, url: str) -> None:
        """Navigate serially and add human-scale jitter between site actions."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            error_dir = USER_HOME / "logs" / "browser-errors"
            error_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            await page.screenshot(path=str(error_dir / f"navigation-{stamp}.png"), full_page=True)
            raise
        await asyncio.sleep(random.uniform(self.min_action_delay_seconds, self.max_action_delay_seconds))

    async def screenshot(self, page: Page, prefix: str) -> str:
        """Save a user-owned diagnostic/confirmation screenshot and return its path."""
        directory = ensure_user_home() / "artifacts" / "screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{prefix}-{stamp}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
