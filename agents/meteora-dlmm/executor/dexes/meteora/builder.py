"""Build chain requests for Meteora DLMM signals."""

from typing import Any, Dict

from common.chain_request import base_request, claim_action


def build(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    action = claim_action(signal["action"])
    req = base_request("meteora", signal, cfg, rails, action=action)

    req["pool_address"] = signal["pool_address"]
    req["position_id"] = signal.get("position_id")

    if action == "open":
        req["bin_range"] = signal.get("bin_range")
        req["liquidity"] = signal.get("liquidity")
    elif action == "add_liquidity":
        if not signal.get("position_id"):
            raise ValueError("meteora add_liquidity requires position_id")
        req["bin_range"] = signal.get("bin_range")
        req["liquidity"] = signal.get("liquidity")
        req["side"] = signal.get("side", "bidirectional")
    elif action == "remove_liquidity":
        if not signal.get("position_id"):
            raise ValueError("meteora remove_liquidity requires position_id")
        req["bps"] = int(signal["bps"])
        req["claim_after"] = bool(signal.get("claim_fees", False))
        req["side"] = signal.get("side", "bidirectional")
    elif action in {"rebalance", "rotate"}:
        if not signal.get("position_id"):
            raise ValueError(f"meteora {action} requires position_id")
        req["bin_range"] = signal.get("bin_range")
        req["liquidity"] = signal.get("liquidity")
        req["side"] = signal.get("side", "bidirectional")
    elif action in {"close", "claim"}:
        if not signal.get("position_id"):
            raise ValueError("meteora close/claim requires position_id")

    return req
