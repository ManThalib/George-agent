"""Journal of all executor decisions."""

import json
from datetime import date
from pathlib import Path

from common.utils import utc_now


JOURNAL_DIR = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/journal")


def append(signal: dict, decision: str, details: dict, rails_hash: str) -> None:
    """Append a decision line to today's journal."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{date.today().isoformat()}.jsonl"
    entry = {
        "timestamp": utc_now(),
        "signal_id": signal.get("signal_id"),
        "action": signal.get("action"),
        "dex": signal.get("dex"),
        "wallet_id": signal.get("wallet_id") or "main",
        "pool_address": signal.get("pool_address"),
        "decision": decision,
        "details": details,
        "rails_hash": rails_hash,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str))
        fh.write("\n")
