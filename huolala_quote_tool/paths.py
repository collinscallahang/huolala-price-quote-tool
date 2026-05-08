from __future__ import annotations

import sys
from pathlib import Path


def runtime_root() -> Path:
    """Return the editable runtime folder for source and packaged builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    return runtime_root() / "config"


def logs_dir() -> Path:
    path = runtime_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
