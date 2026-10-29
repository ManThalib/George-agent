"""Build chain requests for Raydium CLMM signals.

Note: Raydium currently supports open/close/claim only. add_liquidity and
remove_liquidity are accepted by the shared validator for every DEX, but the
Raydium chain handler exports no such action; whether they should be
implemented or rejected earlier is an open owner decision (audit 2026-10-29,
item 4b).
"""

from typing import Any, Dict

from common.chain_request import base_request, claim_action


def build(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    action = claim_action(signal["action"])
    req = base_request("raydium", signal, cfg, rails, action=action)

    req["pool_address"] = signal["pool_address"]
    req["position_id"] = signal.get("position_id")
    # Raydium computes real pending fees/rewards and skips a claim below this
    # USD value (execution_limits.json execution_guards.claim_min_usd).
    req["claim_min_usd"] = rails.get("claim_min_usd", 0)

    if action == "open":
        req["bin_range"] = signal.get("bin_range")
        req["liquidity"] = signal.get("liquidity")
    elif action in {"close", "claim"}:
        if not signal.get("position_id"):
            raise ValueError("raydium close/claim requires position_id")

    return req
