import asyncio

from job_agent.browser.selector_recovery import SelectorRecovery


class FakeLocator:
    def __init__(self, controls, visible=True):  # type: ignore[no-untyped-def]
        self.controls = controls
        self.visible = visible

    async def count(self) -> int:
        return len(self.controls)

    async def is_visible(self) -> bool:
        return self.visible

    def nth(self, index: int):  # type: ignore[no-untyped-def]
        return FakeLocator([self.controls[index]], self.visible)

    async def evaluate(self, _script: str):
        return self.controls[0]


class FakePage:
    def __init__(self) -> None:
        self.meta = {
            "tag": "textarea",
            "aria": "",
            "placeholder": "Сообщение",
            "title": "",
            "name": "",
            "text": "",
        }

    def locator(self, selector: str):  # type: ignore[no-untyped-def]
        if selector == "textarea, [contenteditable=\"true\"]":
            return FakeLocator([self.meta])
        if selector == 'textarea[placeholder="Сообщение"]':
            return FakeLocator([self.meta])
        return FakeLocator([])


def test_semantic_recovery_activates_a_user_owned_selector(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("job_agent.browser.selector_recovery.USER_HOME", tmp_path)
    recovery = SelectorRecovery("sample")

    resolved = asyncio.run(
        recovery.resolve(
            FakePage(),
            "composer",
            ["textarea[aria-label=missing]"],
            ("сообщ", "message"),
            'textarea, [contenteditable="true"]',
        )
    )

    assert resolved is not None
    saved = (tmp_path / "selectors.yaml").read_text(encoding="utf-8")
    assert "runtime_semantic_recovery" in saved
    assert 'textarea[placeholder="Сообщение"]' in saved
