from __future__ import annotations

import unittest
from unittest.mock import patch

from price_quote_tool.automation import AutomationError, PlaywrightQuoteClient


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        self.value += 0.2
        return self.value


class FakePage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


class FakeButtonLocator:
    def __init__(self, modal: "FakeModal") -> None:
        self.modal = modal

    @property
    def last(self) -> "FakeButtonLocator":
        return self

    def filter(self, **_kwargs) -> "FakeButtonLocator":
        return self

    def scroll_into_view_if_needed(self, timeout: int) -> None:
        self.modal.scroll_timeout = timeout

    def click(self, timeout: int) -> None:
        self.modal.clicked = True
        self.modal.click_timeout = timeout


class FakeModal:
    def __init__(self, textarea_value: str, disabled: bool = False) -> None:
        self.textarea_value = textarea_value
        self.disabled = disabled
        self.clicked = False
        self.click_timeout: int | None = None
        self.scroll_timeout: int | None = None

    def evaluate(self, _script: str, payload):
        required_value = payload.get("requiredValue")
        matches = required_value is None or self.textarea_value == required_value
        return {
            "found": True,
            "disabled": self.disabled,
            "textareaMatches": matches,
            "textareaValue": self.textarea_value,
        }

    def locator(self, _selector: str) -> FakeButtonLocator:
        return FakeButtonLocator(self)


class ModalAutomationTests(unittest.TestCase):
    def make_client(self) -> PlaywrightQuoteClient:
        client = PlaywrightQuoteClient.__new__(PlaywrightQuoteClient)
        client._page = FakePage()
        return client

    def test_click_modal_button_requires_textarea_value(self) -> None:
        client = self.make_client()
        modal = FakeModal(textarea_value="")
        clock = FakeClock()

        with patch("price_quote_tool.automation.time.monotonic", clock.monotonic):
            with self.assertRaisesRegex(AutomationError, "地址"):
                client._click_modal_button(modal, "OK", required_textarea_value="target address")

        self.assertFalse(modal.clicked)

    def test_click_modal_button_waits_then_clicks_when_value_matches(self) -> None:
        client = self.make_client()
        modal = FakeModal(textarea_value="target address")
        clock = FakeClock()

        with patch("price_quote_tool.automation.time.monotonic", clock.monotonic):
            client._click_modal_button(modal, "OK", required_textarea_value="target address")

        self.assertTrue(modal.clicked)
        self.assertEqual(modal.scroll_timeout, 2000)
        self.assertEqual(modal.click_timeout, 3000)


if __name__ == "__main__":
    unittest.main()
