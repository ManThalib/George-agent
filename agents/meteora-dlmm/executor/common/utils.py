"""Common utilities for the multi-DEX executor."""

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def rails_hash(rails_path: Path) -> str:
    """Hash of the safety rails file, used to prove rails were checked."""
    if not rails_path.exists():
        return ""
    content = rails_path.read_text(encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
