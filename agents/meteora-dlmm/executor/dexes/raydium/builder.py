"""Build chain requests for Raydium CLMM signals."""

from typing import Any, Dict


def build(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    raw_action = signal["action"]
    action = "claim" if raw_action in {"claim_fees", "claim_rewards"} else raw_action
    slippage = min(int(signal.get("max_slippage_bps", rails.get("max_slippage_bps", 100))), int(rails.get("max_slippage_bps", 100)))

    req: Dict[str, Any] = {
        "dex": "raydium",
        "action": action,
        "mode": "send",
        "rpc_url": cfg["rpc_https_url"],
        "fallback_rpc_urls": cfg.get("rpc_fallback_urls", []),
        "fallback_delay_seconds": cfg.get("rpc_fallback_delay_seconds", 15),
        "wallet_public_key": cfg["wallet_public_key"],
        "max_slippage_bps": slippage,
        "priority_fee_cap_sol": rails.get("priority_fee_cap_sol", 0.0005),
        "pool_address": signal["pool_address"],
        "position_id": signal.get("position_id"),
    }

    if action == "open":
        req["bin_range"] = signal.get("bin_range")
        req["liquidity"] = signal.get("liquidity")
    elif action in {"close", "claim"}:
        if not signal.get("position_id"):
            raise ValueError("raydium close/claim requires position_id")
        req["position_id"] = signal["position_id"]

    return req
