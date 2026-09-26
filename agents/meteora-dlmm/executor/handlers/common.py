"""Shared dry-run helpers for all DEX handlers."""

import uuid
from typing import Any, Dict


def dry_run_result(action: str, signal: Dict[str, Any], cfg: Dict[str, Any], *, notes: str = "") -> Dict[str, Any]:
    """Return a deterministic dry-run result for a signal."""
    return {
        "status": "dry_run",
        "action": action,
        "dex": signal.get("dex"),
        "pool_address": signal.get("pool_address"),
        "position_id": signal.get("position_id"),
        "mode": cfg.get("mode", "dry_run"),
        "simulated_tx": f"dry-run-{uuid.uuid4().hex[:12]}",
        "notes": notes or f"Would execute {action} on {signal.get('dex')} in {cfg.get('mode')} mode.",
    }
