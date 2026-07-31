"""Constrained, user-owned recovery for drifting Playwright selectors.

The recovery layer never edits Python source or sends a site action.  It may
only activate a selector in ``~/.job-harness/selectors.yaml`` after Playwright
has proved that exactly one visible element matches a declared semantic target.
Adapters still perform their normal fill/click and confirmation checks.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import yaml

from job_agent.config.settings import USER_HOME


class SelectorRecovery:
    """Resolve stable controls, then safely learn one runtime fallback."""

    def __init__(self, site: str) -> None:
        self.site = site
        self.path = USER_HOME / "selectors.yaml"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"sites": {}}
        value = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {"sites": {}}
        return value if isinstance(value, dict) else {"sites": {}}

    def _saved(self, target: str) -> list[str]:
        sites = self._load().get("sites", {})
        entry = sites.get(self.site, {}).get("targets", {}).get(target, {}) if isinstance(sites, dict) else {}
        selectors = entry.get("selectors", []) if isinstance(entry, dict) else []
        return [item for item in selectors if isinstance(item, str) and item.strip()]

    def _activate(self, target: str, selector: str, evidence: dict[str, object]) -> None:
        data = self._load()
        sites = data.setdefault("sites", {})
        site = sites.setdefault(self.site, {})
        targets = site.setdefault("targets", {})
        targets[target] = {
            "selectors": [selector],
            "source": "runtime_semantic_recovery",
            "updated_at": datetime.now(UTC).isoformat(),
            "evidence": evidence,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")

    @staticmethod
    async def _unique_visible(locator: Any) -> Any | None:
        if await locator.count() != 1:
            return None
        return locator if await locator.is_visible() else None

    async def resolve(
        self,
        page: Any,
        target: str,
        primary_selectors: list[str],
        tokens: tuple[str, ...],
        element_selector: str,
    ) -> Any | None:
        """Return one visible control; learn only an unambiguous fallback.

        ``tokens`` define semantic intent (for example, ``('сообщ',
        'message')``).  The discovery inventory exposes only control metadata,
        never the conversation or vacancy body.
        """
        for selector in [*self._saved(target), *primary_selectors]:
            try:
                found = await self._unique_visible(page.locator(selector))
            except Exception:
                continue
            if found is not None:
                return found

        controls = page.locator(element_selector)
        candidates: list[tuple[int, int, dict[str, str]]] = []
        for index in range(await controls.count()):
            control = controls.nth(index)
            try:
                if not await control.is_visible():
                    continue
                meta = await control.evaluate(
                    """el => ({
                        tag: el.tagName.toLowerCase(), aria: el.getAttribute('aria-label') || '',
                        placeholder: el.getAttribute('placeholder') || '', title: el.getAttribute('title') || '',
                        name: el.getAttribute('name') || '', text: (el.innerText || '').trim().slice(0, 160)
                    })"""
                )
            except Exception:
                continue
            if not isinstance(meta, dict):
                continue
            values = " ".join(str(meta.get(key, "")) for key in ("aria", "placeholder", "title", "name", "text")).casefold()
            score = sum(1 for token in tokens if token.casefold() in values)
            candidates.append((score, index, {key: str(value) for key, value in meta.items()}))

        best = [item for item in candidates if item[0] > 0 and item[0] == max(score for score, _, _ in candidates)] if candidates else []
        if len(best) != 1:
            return None
        _, index, meta = best[0]
        selector = self._stable_selector(meta, tokens)
        if selector:
            self._activate(target, selector, {"matched": meta, "candidate_count": len(candidates)})
            recovered = await self._unique_visible(page.locator(selector))
            if recovered is not None:
                return recovered
        # The indexed locator is safe for this one page only. It is still
        # unique in the inspected visible inventory and requires normal site
        # confirmation after the action.
        return controls.nth(index)

    @staticmethod
    def _stable_selector(meta: dict[str, str], tokens: tuple[str, ...]) -> str | None:
        tag = meta.get("tag", "")
        placeholder = meta.get("placeholder", "")
        aria = meta.get("aria", "")
        if tag == "textarea" and placeholder:
            return f'textarea[placeholder="{SelectorRecovery._css_quote(placeholder)}"]'
        if tag == "button" and aria:
            token = next((item for item in tokens if item.casefold() in aria.casefold()), "")
            if token:
                return f'button[aria-label*="{SelectorRecovery._css_quote(token)}" i]'
        return None

    @staticmethod
    def _css_quote(value: str) -> str:
        return re.sub(r'(["\\])', r"\\\1", value)
