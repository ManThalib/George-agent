"""Handler registry for multi-DEX execution."""

from pathlib import Path
from typing import Any, Dict

from . import meteora, raydium, orca


HANDLERS = {
    "meteora": meteora,
    "raydium": raydium,
    "orca": orca,
}


def dispatch(signal: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    dex = signal.get("dex") or "meteora"
    handler = HANDLERS.get(dex)
    if not handler:
        raise ValueError(f"unsupported dex: {dex}")

    action = signal.get("action")
    if action == "open":
        return handler.open_position(signal, cfg)
    if action == "close":
        return handler.close_position(signal, cfg)
    if action in {"claim_fees", "claim_rewards"}:
        return handler.claim_fees(signal, cfg)
    raise ValueError(f"unsupported action: {action}")
