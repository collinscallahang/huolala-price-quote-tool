from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .automation import ThreadedQuoteClient
from .excel_io import build_workbook_job, cell_name
from .models import QuoteResult, QuoteTask
from .site_config import load_site_config


QuoteClientFactory = Callable[[], object]


def configured_path(value: str | None, root_dir: Path, fallback: Path) -> Path:
    if not value:
        return fallback
    path = Path(value)
    if not path.is_absolute():
        path = root_dir / path
    return path


@dataclass
class RunProgress:
    run_id: str
    status: str = "created"
    total_tasks: int = 0
    completed_tasks: int = 0
    success_tasks: int = 0
    failed_tasks: int = 0
    current_task: str = ""
    message: str = ""
    output_dir: str = ""
    result_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "success_tasks": self.success_tasks,
            "failed_tasks": self.failed_tasks,
            "current_task": self.current_task,
            "message": self.message,
            "output_dir": self.output_dir,
            "result_files": self.result_files,
            "warnings": self.warnings,
        }


class BatchRun:
    def __init__(
        self,
        run_id: str,
        excel_paths: list[Path],
        site_url: str,
        retry_count: int,
        root_dir: Path,
        config_path: Path,
        overwrite: bool = False,
        quote_client_factory: QuoteClientFactory | None = None,
    ) -> None:
        self.run_id = run_id
        self.excel_paths = excel_paths
        self.site_url = site_url
        self.retry_count = max(1, retry_count)
        self.root_dir = root_dir
        self.config_path = config_path
        self.overwrite = overwrite
        self.config = load_site_config(config_path)
        output_root = configured_path(
            self.config.get("output_root"),
            root_dir,
            root_dir / "outputs" / "runs",
        )
        self.output_dir = output_root / run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = [
            build_workbook_job(
                path,
                self.output_dir,
                length_mapping=self.config.get("vehicle_length_mapping", {}),
                overwrite=overwrite,
            )
            for path in excel_paths
        ]
        self.tasks = [task for job in self.jobs for task in job.summary.tasks]
        self._task_lookup = {task.task_id: task for task in self.tasks}
        warnings = [warning for job in self.jobs for warning in job.summary.warnings]
        self.progress = RunProgress(
            run_id=run_id,
            status="created",
            total_tasks=len(self.tasks),
            output_dir=str(self.output_dir),
            warnings=warnings,
        )
        self._lock = threading.Lock()
        self._pause = threading.Event()
        self._pause.set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._results: list[QuoteResult] = []
        self._quote_client_factory = quote_client_factory or self._default_quote_client_factory
        self._retained_client: object | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=f"quote-run-{self.run_id}", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._pause.clear()
        with self._lock:
            self.progress.status = "paused"

    def resume(self) -> None:
        self._pause.set()
        with self._lock:
            if self.progress.status == "paused":
                self.progress.status = "running"

    def stop(self) -> None:
        self._stop.set()
        self._pause.set()
        with self._lock:
            self.progress.status = "stopping"

    def snapshot(self) -> dict:
        with self._lock:
            return self.progress.to_dict()

    def result_files(self) -> list[Path]:
        return [job.summary.output_path for job in self.jobs if job.summary.output_path.exists()]

    def _default_quote_client_factory(self) -> ThreadedQuoteClient:
        return ThreadedQuoteClient(self.site_url, self.config, self.root_dir)

    def _run(self) -> None:
        client = None
        started_at = datetime.now().isoformat(timespec="seconds")
        try:
            with self._lock:
                self.progress.status = "running"
                self.progress.message = "正在启动浏览器"
            self._write_progress_snapshot()
            client = self._quote_client_factory()
            if hasattr(client, "open"):
                client.open()

            for row_tasks in self._task_groups():
                if self._stop.is_set():
                    break
                self._pause.wait()
                first_task = row_tasks[0]
                with self._lock:
                    self.progress.status = "running"
                    labels = "、".join(task.vehicle_label for task in row_tasks)
                    self.progress.current_task = f"{first_task.source_file} 第 {first_task.row_index} 行 {labels}"
                self._write_progress_snapshot()

                if hasattr(client, "quote_row"):
                    results = self._quote_row_with_retry(client, row_tasks)
                    for result in results:
                        self._record_result(result)
                else:
                    for task in row_tasks:
                        result = self._quote_with_retry(client, task)
                        self._record_result(result)

            self._save_all()
            status = "stopped" if self._stop.is_set() else "completed"
            with self._lock:
                self.progress.status = status
                self.progress.current_task = ""
                self.progress.message = "运行结束"
                self.progress.result_files = [str(path) for path in self.result_files()]
            self._write_progress_snapshot()
        except Exception as exc:
            self._save_all(best_effort=True)
            with self._lock:
                self.progress.status = "failed"
                self.progress.message = str(exc)
            self._write_progress_snapshot()
        finally:
            keep_browser_open = bool(self.config.get("keep_browser_open_after_run", False))
            if client and keep_browser_open:
                self._retained_client = client
            elif client and hasattr(client, "close"):
                try:
                    client.close()
                except Exception:
                    pass
            self._write_run_summary(started_at)

    def _quote_with_retry(self, client: object, task: QuoteTask) -> QuoteResult:
        last_result: QuoteResult | None = None
        for attempt in range(1, self.retry_count + 1):
            result = client.quote(task)
            result.attempts = attempt
            if result.success:
                return result
            last_result = result
            time.sleep(0.8)
        return last_result or QuoteResult(
            task_id=task.task_id,
            file_id=task.file_id,
            row_index=task.row_index,
            price_col=task.price_col,
            success=False,
            error="未知错误",
            attempts=self.retry_count,
        )

    def _quote_row_with_retry(self, client: object, row_tasks: list[QuoteTask]) -> list[QuoteResult]:
        pending = list(row_tasks)
        final_by_task: dict[str, QuoteResult] = {}
        last_failures: dict[str, QuoteResult] = {}
        for attempt in range(1, self.retry_count + 1):
            row_results = client.quote_row(pending)
            for result in row_results:
                result.attempts = attempt
                if result.success:
                    final_by_task[result.task_id] = result
                else:
                    last_failures[result.task_id] = result
            pending = [task for task in pending if task.task_id not in final_by_task]
            if not pending:
                break
            time.sleep(0.8)

        for task in pending:
            final_by_task[task.task_id] = last_failures.get(
                task.task_id,
                QuoteResult(
                    task_id=task.task_id,
                    file_id=task.file_id,
                    row_index=task.row_index,
                    price_col=task.price_col,
                    success=False,
                    error="未知错误",
                    attempts=self.retry_count,
                ),
            )
        ordered_results = [final_by_task[task.task_id] for task in row_tasks if task.task_id in final_by_task]
        distance_seen = False
        for result in ordered_results:
            if not result.success or result.distance is None:
                continue
            if distance_seen:
                result.distance = None
                result.distance_source = ""
            else:
                distance_seen = True
        return ordered_results

    def _task_groups(self) -> list[list[QuoteTask]]:
        groups: list[list[QuoteTask]] = []
        current_key: tuple[str, int] | None = None
        current_group: list[QuoteTask] = []
        for task in self.tasks:
            key = (task.file_id, task.row_index)
            if current_key is None or key == current_key:
                current_group.append(task)
            else:
                groups.append(current_group)
                current_group = [task]
            current_key = key
        if current_group:
            groups.append(current_group)
        return groups

    def _record_result(self, result: QuoteResult) -> None:
        job = next(job for job in self.jobs if job.summary.file_id == result.file_id)
        job.write_result(result)
        self._results.append(result)
        with self._lock:
            self.progress.completed_tasks += 1
            if result.success:
                self.progress.success_tasks += 1
            else:
                self.progress.failed_tasks += 1
                self.progress.message = result.error
        self._write_progress_snapshot()

    def _write_progress_snapshot(self) -> None:
        path = self.output_dir / "progress.json"
        tmp_path = path.with_suffix(".json.tmp")
        with self._lock:
            snapshot = self.progress.to_dict()
        tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _save_all(self, best_effort: bool = False) -> None:
        for job in self.jobs:
            try:
                job.save()
            except Exception:
                if not best_effort:
                    raise
        self._write_failures()
        self._write_copy_records()

    def _write_failures(self) -> None:
        failures = [result for result in self._results if not result.success]
        if not failures:
            return
        path = self.output_dir / "失败明细.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["task_id", "file_id", "row_index", "price_col", "attempts", "error"],
            )
            writer.writeheader()
            for result in failures:
                writer.writerow(
                    {
                        "task_id": result.task_id,
                        "file_id": result.file_id,
                        "row_index": result.row_index,
                        "price_col": result.price_col or "",
                        "attempts": result.attempts,
                        "error": result.error,
                    }
                )

    def _write_copy_records(self) -> None:
        records: list[dict[str, object]] = []
        for result in self._results:
            if not result.success:
                continue
            task = self._task_lookup.get(result.task_id)
            if not task:
                continue
            if result.distance is not None:
                records.append(
                    self._copy_record(
                        task,
                        result,
                        field_name="路程",
                        page_field="页面「总里程」",
                        value=result.distance,
                        source=result.distance_source,
                        target_cell=cell_name(result.row_index, task.distance_col),
                    )
                )
            if result.price is not None:
                records.append(
                    self._copy_record(
                        task,
                        result,
                        field_name="价格",
                        page_field="右下角结算区「运费一口价」",
                        value=result.price,
                        source=result.price_source,
                        target_cell=cell_name(result.row_index, result.price_col),
                    )
                )
        if not records:
            return
        path = self.output_dir / "复制粘贴记录.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "文件",
                    "供应商",
                    "Excel行",
                    "单元格",
                    "字段",
                    "车型类别",
                    "车长",
                    "页面字段",
                    "页面读取值",
                    "写入值",
                    "来源文本",
                    "任务ID",
                    "复制时间",
                ],
            )
            writer.writeheader()
            writer.writerows(records)

    def _copy_record(
        self,
        task: QuoteTask,
        result: QuoteResult,
        field_name: str,
        page_field: str,
        value: float,
        source: str,
        target_cell: str,
    ) -> dict[str, object]:
        return {
            "文件": task.source_file,
            "供应商": task.supplier_name,
            "Excel行": task.row_index,
            "单元格": target_cell,
            "字段": field_name,
            "车型类别": task.vehicle_type,
            "车长": task.vehicle_label,
            "页面字段": page_field,
            "页面读取值": value,
            "写入值": value,
            "来源文本": source,
            "任务ID": result.task_id,
            "复制时间": result.copied_at,
        }

    def _write_run_summary(self, started_at: str) -> None:
        summary = {
            "run_id": self.run_id,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "progress": self.snapshot(),
            "workbooks": [job.summary.to_dict() for job in self.jobs],
            "results": [result.to_dict() for result in self._results],
        }
        with (self.output_dir / "run_summary.json").open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)


class RunManager:
    def __init__(self, root_dir: str | Path, config_path: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.config_path = Path(config_path)
        self.runs: dict[str, BatchRun] = {}
        self._lock = threading.Lock()

    def create_run(
        self,
        excel_paths: list[Path],
        site_url: str,
        retry_count: int,
        overwrite: bool = False,
    ) -> BatchRun:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._lock:
            while run_id in self.runs:
                run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            run = BatchRun(
                run_id=run_id,
                excel_paths=excel_paths,
                site_url=site_url,
                retry_count=retry_count,
                root_dir=self.root_dir,
                config_path=self.config_path,
                overwrite=overwrite,
            )
            self.runs[run_id] = run
            return run

    def get(self, run_id: str) -> BatchRun:
        if run_id not in self.runs:
            raise KeyError(run_id)
        return self.runs[run_id]
