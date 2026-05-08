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


class AutomationError(RuntimeError):
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
                self._ensure_service_option("快车")
                self._page.wait_for_timeout(1000)
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

    def _recognize_route_address(self, badge_text: str, value: str) -> None:
        if not self._click_route_action_by_index(badge_text, "地址识别"):
            self._click_row_text(badge_text, "地址识别")
        timeout = int(self.config.get("timeouts_ms", {}).get("default", 10000))
        modal = self._page.locator(".ant-modal").filter(has_text="地址识别").last
        modal.wait_for(timeout=timeout)
        textarea = modal.locator("textarea").first
        textarea.click(timeout=3000)
        textarea.fill(value, timeout=3000)
        modal.get_by_text("确认", exact=True).click(timeout=3000)
        try:
            modal.wait_for(state="hidden", timeout=timeout)
        except Exception:
            # Some Ant Design modals remain mounted briefly; wait for the overlay to stop blocking.
            self._page.wait_for_timeout(1000)
        self._resolve_accurate_address_modal()

    def _resolve_accurate_address_modal(self) -> None:
        timeout = int(self.config.get("timeouts_ms", {}).get("address_choice", 3000))
        modal = self._page.locator(".ant-modal").filter(has_text="选择准确地址").last
        try:
            modal.wait_for(timeout=timeout)
        except Exception:
            return

        try:
            modal.evaluate(
                r"""modal => {
                    const normalize = value => (value || "").replace(/\s+/g, " ").trim();
                    const body = modal.querySelector(".ant-modal-body") || modal;
                    const candidates = [...body.querySelectorAll("li, .ant-list-item, [class*='item'], [class*='address'], [class*='option'], div")]
                        .filter(element => {
                            const text = normalize(element.innerText || element.textContent);
                            const rect = element.getBoundingClientRect();
                            return text && !text.includes("选择准确地址") && rect.width > 20 && rect.height > 10;
                        });
                    const first = candidates[0];
                    if (first) {
                        first.scrollIntoView({block: "center", inline: "nearest"});
                        first.click();
                    }
                }"""
            )
        except Exception:
            pass

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
        point = self._page.evaluate(
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
                            x: nextRect.left + nextRect.width / 2,
                            y: nextRect.top + nextRect.height / 2,
                        };
                    }
                }
                return null;
            }""",
            service_name,
        )
        if not point:
            raise AutomationError(f"未找到服务选项：{service_name}")
        self._page.mouse.click(float(point["x"]), float(point["y"]))
        self._page.wait_for_timeout(800)

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
