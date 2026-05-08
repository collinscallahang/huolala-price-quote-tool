from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PriceColumn:
    col_index: int
    vehicle_type: str
    length_raw: str
    vehicle_label: str

    @property
    def display_name(self) -> str:
        return f"{self.vehicle_type} / {self.length_raw}"


@dataclass(frozen=True)
class QuoteTask:
    task_id: str
    file_id: str
    source_file: str
    row_index: int
    supplier_name: str
    origin_address: str
    destination_address: str
    distance_col: int | None
    price_col: int | None
    vehicle_type: str
    vehicle_length: str
    vehicle_label: str
    needs_distance: bool
    needs_price: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteResult:
    task_id: str
    file_id: str
    row_index: int
    price_col: int | None
    success: bool
    price: float | None = None
    distance: float | None = None
    error: str = ""
    attempts: int = 1
    price_source: str = ""
    distance_source: str = ""
    copied_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkbookSummary:
    file_id: str
    source_path: Path
    output_path: Path
    sheet_name: str
    total_rows: int
    price_columns: list[PriceColumn]
    tasks: list[QuoteTask]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_path"] = str(self.source_path)
        data["output_path"] = str(self.output_path)
        return data
