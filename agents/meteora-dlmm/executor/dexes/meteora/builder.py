"""Build chain requests for Meteora DLMM signals."""

from typing import Any, Dict


def build(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    raw_action = signal["action"]
    action = "claim" if raw_action in {"claim_fees", "claim_rewards"} else raw_action
    slippage = min(int(signal.get("max_slippage_bps", rails.get("max_slippage_bps", 100))), int(rails.get("max_slippage_bps", 100)))

    req: Dict[str, Any] = {
        "dex": "meteora",
        "action": action,
        "mode": "send",
        "rpc_url": cfg["rpc_https_url"],
        "fallback_rpc_urls": cfg.get("rpc_fallback_urls", []),
        "fallback_delay_seconds": cfg.get("rpc_fallback_delay_seconds", 15),
        "wallet_public_key": cfg["wallet_public_key"],
        "wallet_id": signal.get("wallet_id") or "main",
        "max_slippage_bps": slippage,
        "priority_fee_cap_sol": rails.get("priority_fee_cap_sol", 0.0005),
        "pool_address": signal["pool_address"],
        "position_id": signal.get("position_id"),
    }

    if action == "open":
        req["bin_range"] = signal.get("bin_range")
        req["liquidity"] = signal.get("liquidity")
    elif action == "add_liquidity":
        if not signal.get("position_id"):
            raise ValueError("meteora add_liquidity requires position_id")
        req["position_id"] = signal["position_id"]
        req["bin_range"] = signal.get("bin_range")
        req["liquidity"] = signal.get("liquidity")
        req["side"] = signal.get("side", "bidirectional")
    elif action == "remove_liquidity":
        if not signal.get("position_id"):
            raise ValueError("meteora remove_liquidity requires position_id")
        req["position_id"] = signal["position_id"]
        req["bps"] = int(signal["bps"])
        req["claim_after"] = bool(signal.get("claim_fees", False))
        req["side"] = signal.get("side", "bidirectional")
    elif action in {"rebalance", "rotate"}:
        if not signal.get("position_id"):
            raise ValueError(f"meteora {action} requires position_id")
        req["position_id"] = signal["position_id"]
        req["bin_range"] = signal.get("bin_range")
        req["liquidity"] = signal.get("liquidity")
        req["side"] = signal.get("side", "bidirectional")
    elif action in {"close", "claim"}:
        if not signal.get("position_id"):
            raise ValueError("meteora close/claim requires position_id")
        req["position_id"] = signal["position_id"]

    return req
