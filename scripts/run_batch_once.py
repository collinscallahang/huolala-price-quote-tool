from __future__ import annotations

import argparse
import json
from pathlib import Path

from price_quote_tool.runner import BatchRun
from price_quote_tool.site_config import load_site_config


def configured_path(value: str | None, root_dir: Path) -> Path:
    if not value:
        return root_dir
    path = Path(value)
    return path if path.is_absolute() else root_dir / path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one batch quote job.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--retry-count", type=int, default=2)
    parser.add_argument("--site-url", default="")
    parser.add_argument("--root-dir", default=".")
    parser.add_argument("--config", default="configs/site.huolala.json")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("excel_paths", nargs="*")
    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    config_path = (root_dir / args.config).resolve()
    config = load_site_config(config_path)
    excel_paths = [Path(path).resolve() for path in args.excel_paths]
    if not excel_paths:
        input_dir = configured_path(config.get("default_input_dir"), root_dir)
        excel_paths = sorted(path.resolve() for path in input_dir.glob("*.xlsx") if not path.name.startswith("~$"))
    if not excel_paths:
        raise SystemExit("未找到 Excel 文件")

    run = BatchRun(
        run_id=args.run_id,
        excel_paths=excel_paths,
        site_url=args.site_url or config.get("default_url", ""),
        retry_count=args.retry_count,
        root_dir=root_dir,
        config_path=config_path,
        overwrite=args.overwrite,
    )
    run._run()
    print(json.dumps(run.snapshot(), ensure_ascii=False))


if __name__ == "__main__":
    main()
