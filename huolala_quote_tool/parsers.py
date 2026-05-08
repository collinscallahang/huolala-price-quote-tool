from __future__ import annotations

import re


def parse_total_distance(text: str) -> float:
    patterns = [
        r"总\s*里程\s*([0-9]+(?:\.[0-9]+)?)\s*公里",
        r"总\s*里程\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*km",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    raise ValueError("未找到页面文本中的总里程")


def parse_fixed_price(text: str) -> float:
    patterns = [
        r"运费\s*一口价\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"一口价\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    raise ValueError("未找到页面文本中的运费一口价")
