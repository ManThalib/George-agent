"""Pending review queue for dust swap_to_usdc signals."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.utils import load_json, write_json


DATA_DIR = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/data/dust_swaps")
PENDING_DIR = DATA_DIR / "pending"
REVIEWED_DIR = DATA_DIR / "reviewed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWED_DIR.mkdir(parents=True, exist_ok=True)


def queue(signal: Dict[str, Any]) -> Path:
    """Queue a validated swap_to_usdc signal for manual review."""
    _ensure_dirs()
    path = PENDING_DIR / f"{signal['signal_id']}.json"
    record = {
        "signal_id": signal["signal_id"],
        "signal": signal,
        "status": "pending_review",
        "queued_at": _now(),
    }
    write_json(path, record)
    return path


def get(signal_id: str) -> Optional[Dict[str, Any]]:
    """Load a pending or reviewed record by signal id."""
    _ensure_dirs()
    for dir_ in (PENDING_DIR, REVIEWED_DIR):
        path = dir_ / f"{signal_id}.json"
        if path.exists():
            return load_json(path)
    return None


def list_pending() -> List[Dict[str, Any]]:
    """Return all records currently awaiting review."""
    _ensure_dirs()
    records: List[Dict[str, Any]] = []
    for path in sorted(PENDING_DIR.glob("*.json")):
        try:
            records.append(load_json(path))
        except Exception:
            continue
    return records


def update_status(signal_id: str, status: str) -> Path:
    """Move a pending record to the reviewed folder with the given status."""
    _ensure_dirs()
    src = PENDING_DIR / f"{signal_id}.json"
    if not src.exists():
        raise FileNotFoundError(f"no pending dust swap record for {signal_id}")
    record = load_json(src)
    record["status"] = status
    record["reviewed_at"] = _now()
    dest = REVIEWED_DIR / f"{signal_id}.json"
    write_json(dest, record)
    src.unlink()
    return dest
