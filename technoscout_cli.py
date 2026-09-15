"""Lightweight path helpers shared by command-line utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def load_config(path: str) -> dict[str, Any]:
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = ROOT / value
    with value.open(encoding="utf-8") as handle:
        return json.load(handle)


def database_path(cfg: dict[str, Any]) -> Path:
    value = Path(str(cfg.get("database", "data/technoscout.db"))).expanduser()
    return value if value.is_absolute() else ROOT / value
