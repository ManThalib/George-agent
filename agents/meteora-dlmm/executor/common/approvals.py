"""Pending approval store for confirm_each mode."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


APPROVALS_DIR = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/approvals")
PENDING_DIR = APPROVALS_DIR / "pending"
APPROVED_DIR = APPROVALS_DIR / "approved"
REJECTED_DIR = APPROVALS_DIR / "rejected"
FAILED_VERIFY_DIR = APPROVALS_DIR / "failed_verify"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_request(signal: Dict[str, Any], simulation: Dict[str, Any]) -> Path:
    """Store a pending approval request."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = PENDING_DIR / f"{signal['signal_id']}.json"
    record = {
        "signal_id": signal["signal_id"],
        "signal": signal,
        "simulation": simulation,
        "requested_at": _now(),
    }
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return path


def load_request(signal_id: str) -> Optional[Dict[str, Any]]:
    path = PENDING_DIR / f"{signal_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def move_to(signal_id: str, dest_dir: Path) -> Path:
    src = PENDING_DIR / f"{signal_id}.json"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{signal_id}.json"
    if dest.exists():
        dest.unlink()
    shutil.move(str(src), str(dest))
    return dest


def list_pending() -> Dict[str, Dict[str, Any]]:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    pending = {}
    for p in PENDING_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            pending[data.get("signal_id", p.stem)] = data
        except Exception:
            continue
    return pending
