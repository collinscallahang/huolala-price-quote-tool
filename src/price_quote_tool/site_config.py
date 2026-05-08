from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DEFAULT_LENGTH_MAPPING = {
    "9.6": "9米6",
    "9.60": "9米6",
    "12.5&13": "13米",
    "12.5": "13米",
    "13": "13米",
    "16.5&17.5": "17米5",
    "17.5": "17米5",
}

DEFAULT_REQUIREMENT_MAPPING = {
    "厢式货车": ["厢式货车"],
    "飞翼门": ["飞翼车"],
    "飞翼车": ["飞翼车"],
}

REQUIREMENT_SPLIT_RE = re.compile(r"[&＆/／、,，+]+")


def load_site_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_site_config(path: str | Path, config: dict[str, Any]) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def vehicle_label_for(length_raw: Any, mapping: dict[str, str] | None = None) -> str:
    raw = normalize_text(length_raw)
    active_mapping = {**DEFAULT_LENGTH_MAPPING, **(mapping or {})}
    if raw in active_mapping:
        return active_mapping[raw]

    numeric = raw.replace("米", ".").replace("m", "").replace("M", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", numeric):
        integer, _, decimal = numeric.partition(".")
        return f"{integer}米{decimal}" if decimal else f"{integer}米"

    if "&" in raw:
        last = raw.split("&")[-1].strip()
        return vehicle_label_for(last, active_mapping)

    return raw


def vehicle_requirement_labels_for(vehicle_type: Any, mapping: dict[str, Any] | None = None) -> list[str]:
    raw = normalize_text(vehicle_type)
    if not raw:
        return []

    active_mapping = {**DEFAULT_REQUIREMENT_MAPPING, **(mapping or {})}
    labels: list[str] = []

    parts = [part.strip() for part in REQUIREMENT_SPLIT_RE.split(raw) if part.strip()]
    for part in parts or [raw]:
        add_labels(labels, active_mapping.get(part, [part]))
    return labels


def add_labels(target: list[str], values: Any) -> None:
    if isinstance(values, str):
        values = [values]
    for value in values or []:
        label = normalize_text(value)
        if label and label not in target:
            target.append(label)


def render_template(template: str, data: dict[str, Any]) -> str:
    rendered = template
    for key, value in data.items():
        rendered = rendered.replace("{" + key + "}", normalize_text(value))
    return rendered
