from __future__ import annotations

import re
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import QuoteResult, QuoteTask
from .site_config import render_template, vehicle_requirement_labels_for


def normalize_address_for_match(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    text = re.sub(r"(选择|确认)$", "", text)
    text = text.replace("，", ",").replace("（", "(").replace("）", ")")
    return text.strip().lower()


class AutomationError(RuntimeError):
    pass


class AddressConfirmationRequired(AutomationError):
    pass


class ThreadedQuoteClient:
    """Run Playwright's sync API on a dedicated thread.

    FastAPI may execute handlers while an asyncio loop is active. Playwright's
    sync API refuses that context, so all browser work is funneled through a
    plain worker thread.
    """

    def __init__(self, site_url: str, config: dict[str, Any], root_dir: str | Path):
        self.site_url = site_url
        self.config = config
        self.root_dir = root_dir
        self._requests: queue.Queue[tuple[str, tuple[Any, ...], dict[str, Any], queue.Queue]] = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._worker, name="playwright-quote-client", daemon=True)
        self._thread.start()

    def open(self) -> None:
        return self._call("open")

    def quote(self, task: QuoteTask) -> QuoteResult:
        return self._call("quote", task)

    def quote_row(self, tasks: list[QuoteTask]) -> list[QuoteResult]:
        return self._call("quote_row", tasks)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put(("__close__", (), {}, result_queue))
        status, payload = result_queue.get()
        self._thread.join(timeout=10)
        if status == "error":
            raise payload

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise AutomationError("浏览器客户端已关闭")
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put((method, args, kwargs, result_queue))
        status, payload = result_queue.get()
        if status == "error":
            raise payload
        return payload

    def _worker(self) -> None:
        client: PlaywrightQuoteClient | None = None
        while True:
            method, args, kwargs, result_queue = self._requests.get()
            if method == "__close__":
                try:
                    if client:
                        client.close()
                    result_queue.put(("ok", None))
                except Exception as exc:
                    result_queue.put(("error", exc))
                return
            try:
                if client is None:
                    client = PlaywrightQuoteClient(self.site_url, self.config, self.root_dir)
                result_queue.put(("ok", getattr(client, method)(*args, **kwargs)))
            except Exception as exc:
                result_queue.put(("error", exc))


class PlaywrightQuoteClient:
    def __init__(self, site_url: str, config: dict[str, Any], root_dir: str | Path):
        self.site_url = site_url or config.get("default_url", "")
        self.config = config
        self.root_dir = Path(root_dir)
        self._playwright = None
        self._context = None
        self._page = None

    def open(self) -> None:
        self._ensure_browser()
        if self.site_url:
            self._goto_site()

    def close(self) -> None:
        if self._context:
            try:
                storage_path = self.root_dir / "data" / "browser-storage-state.json"
                storage_path.parent.mkdir(parents=True, exist_ok=True)
                self._context.storage_state(path=str(storage_path))
            except Exception:
                pass
            self._context.close()
        if self._playwright:
            self._playwright.stop()
        self._context = None
        self._playwright = None
        self._page = None

    def quote(self, task: QuoteTask) -> QuoteResult:
        self._ensure_browser()
        data = task.to_dict()
        last_error = ""
        try:
            extracted: dict[str, Any] = {"price": None, "distance": None, "price_source": "", "distance_source": ""}
            for step in self.config.get("workflow", []):
                step_result = self._execute_step(step, data)
                if step_result:
                    extracted.update(step_result)

            if task.needs_price and extracted.get("price") is None:
                raise AutomationError("未读取到运费一口价")
            if task.needs_distance and extracted.get("distance") is None:
                raise AutomationError("未读取到总里程")

            return QuoteResult(
                task_id=task.task_id,
                file_id=task.file_id,
                row_index=task.row_index,
                price_col=task.price_col,
                success=True,
                price=extracted.get("price"),
                distance=extracted.get("distance"),
                price_source=str(extracted.get("price_source") or ""),
                distance_source=str(extracted.get("distance_source") or ""),
                copied_at=datetime.now().isoformat(timespec="seconds"),
            )
        except Exception as exc:
            last_error = str(exc)
            self._capture_failure_artifacts(task)
            return QuoteResult(
                task_id=task.task_id,
                file_id=task.file_id,
                row_index=task.row_index,
                price_col=task.price_col,
                success=False,
                error=last_error,
            )

    def quote_row(self, tasks: list[QuoteTask]) -> list[QuoteResult]:
        if not tasks:
            return []
        self._ensure_browser()
        first = tasks[0]
        results: list[QuoteResult] = []
        distance_copied = False
        try:
            self._goto_site(reset=True)
            self._recognize_route_address("发", first.origin_address)
            self._recognize_route_address("收", first.destination_address)
            self._page.wait_for_timeout(int(self.config.get("timeouts_ms", {}).get("after_address", 1200)))
        except Exception as exc:
            for task in tasks:
                self._capture_failure_artifacts(task)
                results.append(
                    QuoteResult(
                        task_id=task.task_id,
                        file_id=task.file_id,
                        row_index=task.row_index,
                        price_col=task.price_col,
                        success=False,
                        error=str(exc),
                    )
                )
            return results

        for task in tasks:
            try:
                self._click_vehicle_label(task.vehicle_label)
                self._ensure_vehicle_requirements(task.vehicle_type)
                self._page.wait_for_timeout(int(self.config.get("timeouts_ms", {}).get("after_vehicle", 800)))
                extracted = self._extract_huolala_summary()
                if task.needs_price and extracted.get("price") is None:
                    raise AutomationError("未读取到运费一口价")
                if task.needs_distance and not distance_copied and extracted.get("distance") is None:
                    raise AutomationError("未读取到总里程")

                result_distance = None
                result_distance_source = ""
                if task.needs_distance and not distance_copied and extracted.get("distance") is not None:
                    result_distance = extracted.get("distance")
                    result_distance_source = str(extracted.get("distance_source") or "")
                    distance_copied = True

                results.append(
                    QuoteResult(
                        task_id=task.task_id,
                        file_id=task.file_id,
                        row_index=task.row_index,
                        price_col=task.price_col,
                        success=True,
                        price=extracted.get("price"),
                        distance=result_distance,
                        price_source=str(extracted.get("price_source") or ""),
                        distance_source=result_distance_source,
                        copied_at=datetime.now().isoformat(timespec="seconds"),
                    )
                )
            except Exception as exc:
                self._capture_failure_artifacts(task)
                results.append(
                    QuoteResult(
                        task_id=task.task_id,
                        file_id=task.file_id,
                        row_index=task.row_index,
                        price_col=task.price_col,
                        success=False,
                        error=str(exc),
                    )
                )
        return results

    def _capture_failure_artifacts(self, task: QuoteTask) -> None:
        if not self._page:
            return
        debug_dir = self.root_dir / "outputs" / "debug_failures"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{task.task_id}")
        try:
            (debug_dir / f"{stem}.txt").write_text(self._page.locator("body").inner_text(timeout=3000), encoding="utf-8")
        except Exception:
            pass
        try:
            self._page.screenshot(path=str(debug_dir / f"{stem}.png"), full_page=True)
        except Exception:
            pass

    def _ensure_browser(self) -> None:
        if self._page:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise AutomationError("缺少 playwright，请先安装依赖并运行 playwright install") from exc

        browser_config = self.config.get("browser", {})
        self._playwright = sync_playwright().start()
        user_data_dir = self.root_dir / browser_config.get("user_data_dir", "data/browser-profile")
        user_data_dir.mkdir(parents=True, exist_ok=True)
        viewport = browser_config.get("viewport", {"width": 1440, "height": 1000})
        launch_options = {
            "headless": bool(browser_config.get("headless", False)),
            "viewport": viewport,
            "ignore_https_errors": bool(browser_config.get("ignore_https_errors", True)),
        }
        channel = browser_config.get("channel")
        if channel:
            launch_options["channel"] = channel

        self._context = self._playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            **launch_options,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def _execute_step(self, step: dict[str, Any], data: dict[str, Any]) -> dict[str, Any] | None:
        action = step["action"]
        optional = bool(step.get("optional", False))
        try:
            if action == "goto":
                self._goto_site(reset=True)
                return None
            if action == "fill":
                self._page.locator(step["selector"]).fill(render_template(step["value"], data))
                return None
            if action == "click":
                self._page.locator(step["selector"]).click()
                return None
            if action == "click_text":
                text = render_template(step["text"], data)
                self._page.get_by_text(text, exact=bool(step.get("exact", True))).click()
                return None
            if action == "click_row_text":
                self._click_row_text(step["row_badge_text"], step["text"])
                return None
            if action == "fill_route_address":
                value = render_template(step["value"], data)
                self._fill_route_address(step["badge_text"], value)
                return None
            if action == "recognize_route_address":
                value = render_template(step["value"], data)
                self._recognize_route_address(step["badge_text"], value)
                return None
            if action == "click_vehicle_label":
                label = render_template(step["value"], data)
                self._click_vehicle_label(label)
                return None
            if action == "ensure_vehicle_requirements":
                vehicle_type = render_template(step["value"], data)
                self._ensure_vehicle_requirements(vehicle_type)
                return None
            if action == "ensure_service_option":
                service_name = render_template(step["value"], data)
                self._ensure_service_option(service_name)
                return None
            if action == "wait":
                time.sleep(float(step.get("ms", 500)) / 1000)
                return None
            if action == "wait_for":
                self._page.locator(step["selector"]).wait_for()
                return None
            if action == "extract_huolala_summary":
                return self._extract_huolala_summary()
            raise AutomationError(f"不支持的自动化动作：{action}")
        except Exception:
            if optional:
                return None
            raise

    def _fill_route_address(self, badge_text: str, value: str) -> None:
        rows = self._candidate_rows(badge_text)
        for row in rows:
            fields = row.locator("textarea, input, [contenteditable='true']")
            try:
                count = min(fields.count(), 4)
            except Exception:
                count = 0
            for index in range(count):
                field = fields.nth(index)
                try:
                    field.click(timeout=1500)
                    field.fill(value, timeout=1500)
                    return
                except Exception:
                    try:
                        field.click(timeout=1500)
                        self._page.keyboard.press("Control+A")
                        self._page.keyboard.type(value)
                        return
                    except Exception:
                        continue

            address_block = row.get_by_text(badge_text, exact=True)
            try:
                address_block.click(timeout=1000)
                self._page.keyboard.press("Control+A")
                self._page.keyboard.type(value)
                return
            except Exception:
                continue
        if self._fill_route_address_by_index(badge_text, value):
            return
        raise AutomationError(f"未找到路线地址输入行：{badge_text}")

    def _click_row_text(self, row_badge_text: str, text: str) -> None:
        rows = self._candidate_rows(row_badge_text)
        for row in rows:
            try:
                row.get_by_text(text, exact=True).click(timeout=1500)
                return
            except Exception:
                continue
        if self._click_route_action_by_index(row_badge_text, text):
            return
        raise AutomationError(f"未在 {row_badge_text} 行找到按钮：{text}")

    def _route_index(self, badge_text: str) -> int:
        return 0 if badge_text in {"发", "起", "发货"} else 1

    def _fill_route_address_by_index(self, badge_text: str, value: str) -> bool:
        route_index = self._route_index(badge_text)
        selectors = [
            "input[placeholder*='选择省市区']",
            "textarea[placeholder*='选择省市区']",
            "input[placeholder*='地址']",
            "textarea[placeholder*='地址']",
        ]
        for selector in selectors:
            locator = self._page.locator(selector)
            try:
                count = locator.count()
            except Exception:
                count = 0
            if count <= route_index:
                continue
            field = locator.nth(route_index)
            try:
                field.scroll_into_view_if_needed(timeout=1500)
                field.click(timeout=2000)
                field.fill(value, timeout=3000)
                return True
            except Exception:
                try:
                    field.click(timeout=2000)
                    self._page.keyboard.press("Control+A")
                    self._page.keyboard.type(value)
                    return True
                except Exception:
                    continue
        return False

    def _click_route_action_by_index(self, badge_text: str, text: str) -> bool:
        route_index = self._route_index(badge_text)
        locator = self._page.get_by_text(text, exact=True)
        try:
            if locator.count() <= route_index:
                return False
            locator.nth(route_index).click(timeout=2000)
            return True
        except Exception:
            return False

    def _modal_textarea_state(self, modal: Any) -> dict[str, Any]:
        return modal.evaluate(
            r"""modal => {
                const textarea = modal.querySelector("textarea");
                if (!textarea) return { found: false, ready: false, value: "", rect: null };
                const style = window.getComputedStyle(textarea);
                const rect = textarea.getBoundingClientRect();
                const ready = style.display !== "none" &&
                    style.visibility !== "hidden" &&
                    rect.width > 0 &&
                    rect.height > 0 &&
                    !textarea.disabled &&
                    !textarea.readOnly;
                return {
                    found: true,
                    ready,
                    value: textarea.value || "",
                    rect: {
                        left: rect.left,
                        top: rect.top,
                        width: rect.width,
                        height: rect.height
                    }
                };
            }"""
        )

    def _wait_for_modal_textarea_ready(self, modal: Any, timeout_ms: int = 6000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        previous_rect: dict[str, float] | None = None
        stable_samples = 0
        last_state: dict[str, Any] = {"found": False, "ready": False}
        while time.monotonic() < deadline:
            last_state = self._modal_textarea_state(modal)
            rect = last_state.get("rect")
            if last_state.get("ready") and rect:
                if previous_rect and all(abs(float(rect[key]) - float(previous_rect[key])) <= 0.5 for key in rect):
                    stable_samples += 1
                else:
                    stable_samples = 1
                previous_rect = rect
                if stable_samples >= 3:
                    return
            else:
                stable_samples = 0
                previous_rect = None
            self._page.wait_for_timeout(120)
        if not last_state.get("found"):
            raise AutomationError("地址识别弹窗未找到地址输入框")
        raise AutomationError("地址识别弹窗地址输入框一直未稳定")

    def _modal_textarea_matches(self, modal: Any, value: str) -> bool:
        return bool(
            modal.evaluate(
                r"""(modal, value) => {
                    const textarea = modal.querySelector("textarea");
                    return Boolean(textarea && textarea.value === value);
                }""",
                value,
            )
        )

    def _wait_for_modal_textarea_value(self, modal: Any, value: str, timeout_ms: int = 2500) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._modal_textarea_matches(modal, value):
                return True
            self._page.wait_for_timeout(100)
        return False

    def _paste_modal_textarea_value(self, modal: Any, value: str) -> bool:
        return bool(
            modal.evaluate(
                r"""(modal, value) => {
                    const textarea = modal.querySelector("textarea");
                    if (!textarea || textarea.disabled || textarea.readOnly) return false;
                    const win = textarea.ownerDocument.defaultView || window;
                    const setter = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, "value")?.set;
                    const setValue = text => {
                        if (setter) {
                            setter.call(textarea, text);
                        } else {
                            textarea.value = text;
                        }
                    };
                    textarea.focus({ preventScroll: true });
                    textarea.select();
                    try {
                        const data = new DataTransfer();
                        data.setData("text/plain", value);
                        textarea.dispatchEvent(new ClipboardEvent("paste", {
                            bubbles: true,
                            cancelable: true,
                            clipboardData: data
                        }));
                    } catch (_error) {
                        // Some browser builds do not allow constructing ClipboardEvent with data.
                    }
                    try {
                        textarea.dispatchEvent(new InputEvent("beforeinput", {
                            bubbles: true,
                            cancelable: true,
                            inputType: "insertFromPaste",
                            data: value
                        }));
                    } catch (_error) {
                    }
                    setValue(value);
                    textarea.dispatchEvent(new InputEvent("input", {
                        bubbles: true,
                        cancelable: true,
                        inputType: "insertFromPaste",
                        data: value
                    }));
                    textarea.dispatchEvent(new Event("change", { bubbles: true }));
                    return textarea.value === value;
                }""",
                value,
            )
        )

    def _keyboard_insert_modal_textarea_value(self, modal: Any, value: str) -> bool:
        textarea = modal.locator("textarea").first
        try:
            textarea.click(timeout=4000)
        except Exception:
            focused = bool(
                modal.evaluate(
                    r"""modal => {
                        const textarea = modal.querySelector("textarea");
                        if (!textarea || textarea.disabled || textarea.readOnly) return false;
                        textarea.focus({ preventScroll: true });
                        return document.activeElement === textarea;
                    }"""
                )
            )
            if not focused:
                return False
        try:
            self._page.keyboard.press("Control+A")
            self._page.keyboard.press("Backspace")
            self._page.wait_for_timeout(120)
            self._page.keyboard.insert_text(value)
            return True
        except Exception:
            return False

    def _fill_modal_textarea(self, modal: Any, value: str) -> None:
        modal.locator("textarea").first.wait_for(state="visible", timeout=5000)
        for attempt in range(4):
            self._wait_for_modal_textarea_ready(modal)
            if self._paste_modal_textarea_value(modal, value):
                self._page.wait_for_timeout(350)
                if self._wait_for_modal_textarea_value(modal, value, timeout_ms=1500):
                    self._page.wait_for_timeout(400)
                    if self._modal_textarea_matches(modal, value):
                        return
            if self._keyboard_insert_modal_textarea_value(modal, value):
                self._page.wait_for_timeout(350)
                if self._wait_for_modal_textarea_value(modal, value, timeout_ms=1500):
                    self._page.wait_for_timeout(400)
                    if self._modal_textarea_matches(modal, value):
                        return
            self._page.wait_for_timeout(250 + attempt * 250)
        raise AutomationError("地址识别弹窗地址写入后未生效")

    def _modal_button_state(self, modal: Any, text: str, required_textarea_value: str | None = None) -> dict[str, Any]:
        return modal.evaluate(
            r"""(modal, payload) => {
                const normalize = text => (text || "").replace(/\s+/g, "").trim();
                const target = normalize(payload.text);
                const buttons = [...modal.querySelectorAll("button")];
                const button = buttons.find(item => normalize(item.innerText || item.textContent) === target);
                const textarea = modal.querySelector("textarea");
                const textareaValue = textarea ? textarea.value || "" : "";
                const textareaMatches = payload.requiredValue == null || textareaValue === payload.requiredValue;
                if (!button) return { found: false, disabled: true, textareaMatches, textareaValue };
                const disabled = button.disabled ||
                    button.getAttribute("aria-disabled") === "true" ||
                    String(button.className || "").includes("disabled");
                return { found: true, disabled, textareaMatches, textareaValue };
            }""",
            {"text": text, "requiredValue": required_textarea_value},
        )

    def _click_modal_button(self, modal: Any, text: str, required_textarea_value: str | None = None) -> None:
        deadline = time.monotonic() + 10
        ready_since: float | None = None
        last_state: dict[str, Any] = {"found": False, "disabled": True, "textareaMatches": False}
        while time.monotonic() < deadline:
            last_state = self._modal_button_state(modal, text, required_textarea_value)
            ready = (
                last_state.get("found")
                and not last_state.get("disabled")
                and last_state.get("textareaMatches")
            )
            if ready:
                if ready_since is None:
                    ready_since = time.monotonic()
                elif time.monotonic() - ready_since >= 0.5:
                    break
            else:
                ready_since = None
            self._page.wait_for_timeout(150)
        if not last_state.get("found"):
            raise AutomationError(f"地址识别弹窗未找到按钮：{text}")
        if required_textarea_value is not None and not last_state.get("textareaMatches"):
            raise AutomationError("地址识别弹窗地址尚未成功写入，已取消点击确认")
        if last_state.get("disabled"):
            raise AutomationError(f"地址识别弹窗按钮不可用：{text}")

        button = modal.locator("button").filter(has_text=text).last
        try:
            button.scroll_into_view_if_needed(timeout=2000)
            self._page.wait_for_timeout(200)
            button.click(timeout=3000)
        except Exception:
            clicked = modal.evaluate(
                r"""(modal, payload) => {
                    const normalize = text => (text || "").replace(/\s+/g, "").trim();
                    const target = normalize(payload.text);
                    const textarea = modal.querySelector("textarea");
                    if (payload.requiredValue != null && (!textarea || textarea.value !== payload.requiredValue)) {
                        return false;
                    }
                    const button = [...modal.querySelectorAll("button")]
                        .find(item => normalize(item.innerText || item.textContent) === target);
                    if (!button || button.disabled || button.getAttribute("aria-disabled") === "true") return false;
                    button.click();
                    return true;
                }""",
                {"text": text, "requiredValue": required_textarea_value},
            )
            if not clicked:
                raise

    def _dismiss_modal(self, modal: Any, timeout: int = 3000) -> None:
        try:
            modal.locator(".ant-modal-close, button[aria-label='Close']").first.click(timeout=1000, force=True)
        except Exception:
            try:
                self._page.keyboard.press("Escape")
            except Exception:
                return
        try:
            modal.wait_for(state="hidden", timeout=timeout)
        except Exception:
            self._page.wait_for_timeout(300)

    def _recognize_route_address(self, badge_text: str, value: str) -> None:
        modal = None
        if not self._click_route_action_by_index(badge_text, "地址识别"):
            self._click_row_text(badge_text, "地址识别")
        timeout = int(self.config.get("timeouts_ms", {}).get("default", 10000))
        modal = self._page.locator(".ant-modal").filter(has_text="地址识别").last
        try:
            modal.wait_for(timeout=timeout)
            self._fill_modal_textarea(modal, value)
            self._click_modal_button(modal, "确认", required_textarea_value=value)
            modal.wait_for(state="hidden", timeout=timeout)
        except Exception:
            if modal is not None:
                self._dismiss_modal(modal)
            raise
        self._resolve_accurate_address_modal(value, badge_text)

    def _resolve_accurate_address_modal(self, expected_address: str, badge_text: str) -> None:
        timeout = int(self.config.get("timeouts_ms", {}).get("address_choice", 3000))
        modal = self._page.locator(".ant-modal").filter(has_text="选择准确地址").last
        try:
            modal.wait_for(timeout=timeout)
        except Exception:
            return

        expected = normalize_address_for_match(expected_address)
        candidates: list[str] = []
        try:
            candidates = modal.evaluate(
                r"""modal => {
                    const normalize = value => (value || "").replace(/\s+/g, " ").trim();
                    const body = modal.querySelector(".ant-modal-body") || modal;
                    const elements = [...body.querySelectorAll("li, .ant-list-item, [class*='item'], [class*='address'], [class*='option'], div")]
                        .filter(element => {
                            const text = normalize(element.innerText || element.textContent);
                            const rect = element.getBoundingClientRect();
                            return text && !text.includes("选择准确地址") && rect.width > 20 && rect.height > 10;
                        });
                    const seen = new Set();
                    return elements
                        .map(element => normalize(element.innerText || element.textContent))
                        .filter(text => {
                            if (seen.has(text)) return false;
                            seen.add(text);
                            return true;
                        });
                }"""
            )
        except Exception:
            candidates = []

        matched_text = next((text for text in candidates if normalize_address_for_match(text) == expected), "")
        if not matched_text:
            choices = "；".join(candidates[:3]) if candidates else "未读取到候选地址"
            raise AddressConfirmationRequired(
                f"地址需人工确认：{badge_text} 地址「{expected_address}」未找到完全一致候选。候选：{choices}"
            )

        clicked = False
        try:
            clicked = bool(
                modal.evaluate(
                    r"""(modal, matchedText) => {
                        const normalize = value => (value || "").replace(/\s+/g, " ").trim();
                        const body = modal.querySelector(".ant-modal-body") || modal;
                        const elements = [...body.querySelectorAll("li, .ant-list-item, [class*='item'], [class*='address'], [class*='option'], div")];
                        const found = elements.find(element => normalize(element.innerText || element.textContent) === matchedText);
                        if (!found) return false;
                        found.scrollIntoView({block: "center", inline: "nearest"});
                        found.click();
                        return true;
                    }""",
                    matched_text,
                )
            )
        except Exception:
            clicked = False
        if not clicked:
            raise AddressConfirmationRequired(f"地址需人工确认：{badge_text} 地址「{expected_address}」无法自动选中完全一致候选")

        modal.get_by_text("选择", exact=True).click(timeout=3000)
        try:
            modal.wait_for(state="hidden", timeout=timeout)
        except Exception:
            self._page.wait_for_timeout(1000)

    def _candidate_rows(self, badge_text: str) -> list[Any]:
        selectors = ["tr", ".el-table__row", "[class*='row']", "[class*='address']", "[class*='route']"]
        rows: list[Any] = []
        for selector in selectors:
            locator = self._page.locator(selector).filter(has_text=badge_text)
            try:
                count = min(locator.count(), 20)
            except Exception:
                count = 0
            for index in range(count):
                rows.append(locator.nth(index))
        return rows

    def _wait_for_quote_form(self) -> None:
        timeout = int(self.config.get("timeouts_ms", {}).get("default", 10000))
        try:
            self._page.get_by_text("货运路线", exact=True).wait_for(timeout=timeout)
        except Exception:
            pass
        self._page.locator("input[placeholder*='选择省市区']").first.wait_for(timeout=timeout)

    def _click_vehicle_label(self, label: str) -> None:
        candidates = [
            lambda: self._page.get_by_label(label, exact=True),
            lambda: self._page.get_by_text(label, exact=True),
            lambda: self._page.locator("label").filter(has_text=label),
            lambda: self._page.locator(".ant-radio-wrapper").filter(has_text=label),
        ]
        for make_locator in candidates:
            try:
                locator = make_locator()
                locator.nth(0).click(timeout=2500)
                return
            except Exception:
                continue
        clicked = self._page.evaluate(
            """label => {
                const labels = [...document.querySelectorAll('label, .ant-radio-wrapper')];
                const found = labels.find(el => (el.innerText || el.textContent || '').trim() === label);
                if (!found) return false;
                found.scrollIntoView({block: 'center'});
                found.click();
                return true;
            }""",
            label,
        )
        if clicked:
            return
        raise AutomationError(f"未找到车型选项：{label}")

    def _goto_site(self, reset: bool = False) -> None:
        timeout = int(self.config.get("timeouts_ms", {}).get("navigation", 60000))
        if reset:
            self._page.goto("about:blank", wait_until="domcontentloaded", timeout=timeout)
        try:
            self._page.goto(self.site_url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            # Some SPA navigations keep connections open; if the quote form is already ready, continue.
            self._wait_for_quote_form()
            return
        self._wait_for_quote_form()

    def _ensure_vehicle_requirements(self, vehicle_type: str) -> None:
        requirements = vehicle_requirement_labels_for(
            vehicle_type,
            self.config.get("vehicle_requirement_mapping", {}),
        )
        for _ in range(3):
            changes = self._page.evaluate(
                r"""requirements => {
                const normalize = value => (value || "").replace(/\s+/g, "").trim();
                const requested = new Set(requirements.map(normalize).filter(Boolean));
                const isVisible = element => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== "hidden" &&
                        style.display !== "none" &&
                        rect.width > 0 &&
                        rect.height > 0;
                };
                const labelText = label => normalize(
                    [...label.childNodes]
                        .filter(node => node.nodeType === Node.TEXT_NODE || node.nodeType === Node.ELEMENT_NODE)
                        .map(node => node.innerText || node.textContent || "")
                        .join("")
                );
                const checkboxLabels = [...document.querySelectorAll("label.ant-checkbox-wrapper, .ant-checkbox-wrapper")]
                    .filter(label => label.querySelector("input[type='checkbox']") && isVisible(label));
                const labels = checkboxLabels.filter(label => labelText(label) !== "该车型无可选要求");
                if (labels.length === 0) return;

                const available = new Set(labels.map(labelText).filter(Boolean));
                const targets = new Set([...requested].filter(text => available.has(text)));
                const changes = [];
                const seenInputs = new Set();
                for (const label of labels) {
                    const input = label.querySelector("input[type='checkbox']");
                    if (!input || seenInputs.has(input)) continue;
                    seenInputs.add(input);

                    const text = labelText(label);
                    const disabled = input.disabled ||
                        input.getAttribute("aria-disabled") === "true" ||
                        String(label.className || "").includes("disabled");
                    if (!text || disabled) continue;

                    const shouldCheck = targets.has(text);
                    if (Boolean(input.checked) !== shouldCheck) {
                        label.scrollIntoView({block: "center", inline: "nearest"});
                        const rect = label.getBoundingClientRect();
                        changes.push({
                            text,
                            checked: Boolean(input.checked),
                            shouldCheck,
                            x: rect.left + Math.min(14, Math.max(4, rect.width / 2)),
                            y: rect.top + rect.height / 2
                        });
                    }
                }
                return changes;
            }""",
                requirements,
            )
            if not changes:
                return
            for change in changes:
                self._page.mouse.click(float(change["x"]), float(change["y"]))
                self._page.wait_for_timeout(150)

    def _ensure_service_option(self, service_name: str) -> None:
        timeout_ms = int(self.config.get("timeouts_ms", {}).get("service_option", 20000))
        deadline = time.monotonic() + timeout_ms / 1000
        last_state = ""
        while True:
            state = self._page.evaluate(
                r"""serviceName => {
                    const normalize = value => (value || "").replace(/\s+/g, "").trim();
                    const isVisible = element => {
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.visibility !== "hidden" &&
                            style.display !== "none" &&
                            rect.width > 0 &&
                            rect.height > 0;
                    };
                    const target = normalize(serviceName);
                    const bodyText = normalize(document.body.innerText || document.body.textContent || "");
                    const titleElements = [...document.querySelectorAll("*")]
                        .filter(isVisible)
                        .filter(element => normalize(element.innerText || element.textContent) === target);

                    for (const title of titleElements) {
                        let node = title;
                        for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                            const text = normalize(node.innerText || node.textContent);
                            if (!text.includes(target) || !text.includes("一口价")) continue;
                            if (text.includes("订单中心") || text.includes("货运路线")) continue;
                            const rect = node.getBoundingClientRect();
                            if (rect.width < 80 || rect.height < 50) continue;
                            node.scrollIntoView({block: "center", inline: "center"});
                            const nextRect = node.getBoundingClientRect();
                            return {
                                status: "found",
                                x: nextRect.left + nextRect.width / 2,
                                y: nextRect.top + nextRect.height / 2,
                            };
                        }
                    }
                    if (bodyText.includes("运费一口价") || bodyText.includes("一口价")) {
                        return {status: "priced"};
                    }
                    if (bodyText.includes("计价中")) {
                        return {status: "pricing"};
                    }
                    return {status: "missing"};
                }""",
                service_name,
            )
            last_state = str(state.get("status") or "")
            if last_state == "found":
                self._page.mouse.click(float(state["x"]), float(state["y"]))
                self._page.wait_for_timeout(800)
                return
            if last_state == "priced":
                # Some routes do not expose a separate service card after pricing; keep the quoted default.
                return
            if time.monotonic() >= deadline:
                break
            self._page.wait_for_timeout(500)
        if last_state == "pricing":
            raise AutomationError(f"地址已识别，但等待{service_name}报价超时")
        raise AutomationError(f"地址已识别，但未找到服务选项：{service_name}")

    def _extract_huolala_summary(self) -> dict[str, Any]:
        timeout_ms = int(self.config.get("timeouts_ms", {}).get("summary", 15000))
        deadline = time.monotonic() + timeout_ms / 1000
        last_result: dict[str, Any] = {"price": None, "distance": None, "price_source": "", "distance_source": ""}
        while True:
            body_text = self._page.locator("body").inner_text(timeout=5000)
            price_value, price_source = settlement_price_with_source(
                body_text,
                self.config.get("extract", {}).get("price_patterns", []),
            )
            distance_value, distance_source = last_number_with_source(
                body_text,
                self.config.get("extract", {}).get("distance_patterns", []),
            )
            last_result = {
                "price": price_value,
                "distance": distance_value,
                "price_source": price_source,
                "distance_source": distance_source,
            }
            if last_result["price"] is not None and "计价中" not in body_text:
                return last_result
            if time.monotonic() >= deadline:
                return last_result
            time.sleep(0.5)


def first_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def last_number(text: str, patterns: list[str]) -> float | None:
    value, _source = last_number_with_source(text, patterns)
    return value


def last_number_with_source(text: str, patterns: list[str]) -> tuple[float | None, str]:
    last: float | None = None
    source = ""
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            last = float(match.group(1).replace(",", ""))
            source = compact_source(match.group(0))
        if last is not None:
            return last, source
    return None, ""


def settlement_price(text: str, patterns: list[str]) -> float | None:
    value, _source = settlement_price_with_source(text, patterns)
    return value


def settlement_price_with_source(text: str, patterns: list[str]) -> tuple[float | None, str]:
    settlement_patterns = [
        r"运费\s*[—\-－]?\s*一口价\s*([0-9,]+(?:\.[0-9]+)?)\s*元[\s\S]{0,160}?总计",
        r"货运券抵扣[\s\S]{0,260}?运费\s*[—\-－]?\s*一口价\s*([0-9,]+(?:\.[0-9]+)?)\s*元",
    ]
    for pattern in settlement_patterns:
        matches = list(re.finditer(pattern, text))
        if matches:
            match = matches[-1]
            return float(match.group(1).replace(",", "")), compact_source(match.group(0))

    anchor = max(text.rfind("货运券抵扣"), text.rfind("总计"), text.rfind("下一步") - 400)
    if anchor > 0:
        value, source = first_number_with_source(text[anchor:], patterns)
        if value is not None:
            return value, source
    return last_number_with_source(text, patterns)


def first_number_with_source(text: str, patterns: list[str]) -> tuple[float | None, str]:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1).replace(",", "")), compact_source(match.group(0))
    return None, ""


def compact_source(text: str, limit: int = 180) -> str:
    compacted = re.sub(r"\s+", " ", text).strip()
    return compacted[:limit]
