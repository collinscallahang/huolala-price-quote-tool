from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .excel_model import VehicleRule
from .parsers import parse_fixed_price, parse_total_distance


ORDER_URL = "https://bfe-epc-web-v.huolala.cn/#/order-center/same-city?from=menu"
KNOWN_VEHICLE_REQUIREMENTS = (
    "厢式货车",
    "飞翼车",
    "平板车",
    "高栏车",
    "冷藏车",
    "栏板车",
)


@dataclass
class VehicleQuote:
    distance: float
    price: float


class HuolalaClient:
    def __init__(
        self,
        log: Callable[[str], None] | None = None,
        user_data_dir: str | Path | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        self.log = log or (lambda message: None)
        self.user_data_dir = Path(user_data_dir) if user_data_dir else Path.home() / ".huolala_quote_tool" / "browser_profile"
        self.timeout_ms = timeout_ms
        self._playwright = None
        self.context = None
        self.page = None
        self._last_origin = ""
        self._last_destination = ""

    def start(self) -> None:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "缺少 Playwright。请先运行：python -m pip install -r requirements.txt，"
                "然后运行：python -m playwright install chromium"
            ) from exc

        self.PlaywrightError = PlaywrightError
        self.PlaywrightTimeoutError = PlaywrightTimeoutError
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()

        errors: list[str] = []
        launch_options = [
            {"channel": "msedge"},
            {},
        ]
        for options in launch_options:
            try:
                browser_name = "Microsoft Edge" if options.get("channel") else "Playwright Chromium"
                self.log(f"正在启动浏览器：{browser_name}")
                self.context = self._playwright.chromium.launch_persistent_context(
                    str(self.user_data_dir),
                    headless=False,
                    viewport={"width": 1440, "height": 960},
                    args=["--start-maximized"],
                    **options,
                )
                break
            except Exception as exc:  # Playwright raises several launch-time subclasses.
                errors.append(str(exc))
                self.context = None

        if self.context is None:
            self.stop()
            raise RuntimeError(
                "无法启动浏览器。请确认已安装 Microsoft Edge，或运行 "
                "python -m playwright install chromium。\n"
                + "\n".join(errors[-2:])
            )

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(self.timeout_ms)

    def open_order_page(self) -> None:
        self._require_page()
        self.log("正在打开货拉拉同城下单页")
        self.page.goto(ORDER_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
        self.page.wait_for_load_state("networkidle", timeout=self.timeout_ms)

    def stop(self) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            self.context = None
            self.page = None
            if self._playwright:
                self._playwright.stop()
            self._playwright = None

    def fill_addresses(self, origin: str, destination: str, reuse_if_same: bool = True) -> bool:
        self._require_page()
        origin = origin.strip()
        destination = destination.strip()
        if reuse_if_same and origin == self._last_origin and destination == self._last_destination:
            self.log("地址与上一行相同，复用当前页面地址")
            return False

        self.log("填写发货地址")
        self._fill_input_by_keywords(["发货地址", "发货地", "发货", "起点", "提货", "装货"], origin, fallback_index=0)
        self.log("填写到货地址")
        self._fill_input_by_keywords(["到货地址", "收货地址", "到货", "目的地", "终点", "卸货"], destination, fallback_index=1)
        self._last_origin = origin
        self._last_destination = destination
        self._wait_for_quote_refresh()
        return True

    def read_distance(self) -> float:
        self._require_page()
        body_text = self.page.locator("body").inner_text(timeout=self.timeout_ms)
        return parse_total_distance(body_text)

    def quote_vehicle(self, rule: VehicleRule) -> float:
        self._require_page()
        self.log(f"切换车型：{rule.name}")
        self._select_car_length(rule.car_length)
        self._set_vehicle_requirements(rule.vehicle_requirements)
        self._wait_for_quote_refresh()
        body_text = self.page.locator("body").inner_text(timeout=self.timeout_ms)
        return parse_fixed_price(body_text)

    def _require_page(self) -> None:
        if self.page is None:
            raise RuntimeError("浏览器尚未启动")

    def _fill_input_by_keywords(self, keywords: Iterable[str], value: str, fallback_index: int) -> None:
        locator = self._find_input_by_keywords(keywords)
        if locator is None:
            locator = self._visible_input_by_index(fallback_index)
        if locator is None:
            raise RuntimeError(f"无法定位地址输入框：{'/'.join(keywords)}")

        locator.click(timeout=5000)
        locator.fill("", timeout=5000)
        locator.type(value, delay=20, timeout=10000)
        self.page.keyboard.press("Enter")
        self._click_first_suggestion()

    def _find_input_by_keywords(self, keywords: Iterable[str]):
        keyword_patterns = [re.compile(re.escape(keyword), re.IGNORECASE) for keyword in keywords]

        for pattern in keyword_patterns:
            for getter_name in ("get_by_placeholder", "get_by_label"):
                try:
                    locator = getattr(self.page, getter_name)(pattern).first
                    if locator.count() and locator.is_visible(timeout=800):
                        return locator
                except Exception:
                    continue

        inputs = self.page.locator("input, textarea")
        try:
            count = inputs.count()
        except Exception:
            return None

        for index in range(count):
            candidate = inputs.nth(index)
            try:
                if not candidate.is_visible(timeout=300):
                    continue
                context = candidate.evaluate(
                    """(el) => {
                        const parts = [
                            el.getAttribute('placeholder') || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('name') || '',
                            el.getAttribute('id') || ''
                        ];
                        let node = el;
                        for (let i = 0; i < 4 && node; i += 1) {
                            if (node.innerText) parts.push(node.innerText.slice(0, 240));
                            node = node.parentElement;
                        }
                        return parts.join(' ');
                    }"""
                )
            except Exception:
                continue
            if any(keyword in context for keyword in keywords):
                return candidate
        return None

    def _visible_input_by_index(self, target_index: int):
        inputs = self.page.locator("input, textarea")
        visible = []
        try:
            count = inputs.count()
        except Exception:
            return None

        for index in range(count):
            candidate = inputs.nth(index)
            try:
                if candidate.is_visible(timeout=300) and candidate.is_enabled(timeout=300):
                    visible.append(candidate)
            except Exception:
                continue
        if len(visible) > target_index:
            return visible[target_index]
        return None

    def _click_first_suggestion(self) -> None:
        selectors = [
            "[role='option']",
            ".el-select-dropdown__item",
            ".ant-select-item-option",
            ".address-item",
            ".address-list-item",
            ".poi-item",
        ]
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.count() and locator.is_visible(timeout=800):
                    locator.click(timeout=1500)
                    return
            except Exception:
                continue

    def _select_car_length(self, car_length: str) -> None:
        if self._click_text(car_length, exact=True):
            return
        for label in ("车长", "车型", "选择车型"):
            if self._click_text(label, exact=False):
                time.sleep(0.4)
                if self._click_text(car_length, exact=True) or self._click_text(car_length, exact=False):
                    return
        raise RuntimeError(f"无法选择车长：{car_length}")

    def _select_vehicle_requirement(self, requirement: str) -> None:
        try:
            checkbox = self.page.get_by_label(re.compile(re.escape(requirement))).first
            if checkbox.count():
                checkbox.check(timeout=1500)
                return
        except Exception:
            pass

        if self._click_text(requirement, exact=True) or self._click_text(requirement, exact=False):
            return

        for label in ("车型要求", "车辆要求", "用车要求"):
            if self._click_text(label, exact=False):
                time.sleep(0.4)
                if self._click_text(requirement, exact=True) or self._click_text(requirement, exact=False):
                    return
        raise RuntimeError(f"无法选择车型要求：{requirement}")

    def _set_vehicle_requirements(self, requirements: Iterable[str]) -> None:
        wanted = {item for item in requirements if item}
        for requirement in KNOWN_VEHICLE_REQUIREMENTS:
            if requirement not in wanted:
                self._uncheck_vehicle_requirement(requirement)
        for requirement in requirements:
            self._select_vehicle_requirement(requirement)

    def _uncheck_vehicle_requirement(self, requirement: str) -> bool:
        try:
            checkbox = self.page.get_by_label(re.compile(re.escape(requirement))).first
            if checkbox.count() and checkbox.is_visible(timeout=500):
                checkbox.uncheck(timeout=1000)
                return True
        except Exception:
            pass
        return False

    def _click_text(self, text: str, exact: bool) -> bool:
        try:
            locator = self.page.get_by_text(text, exact=exact).first
            if locator.count() and locator.is_visible(timeout=1000):
                locator.click(timeout=3000)
                return True
        except Exception:
            return False
        return False

    def _wait_for_quote_refresh(self) -> None:
        self._require_page()
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        time.sleep(0.8)
