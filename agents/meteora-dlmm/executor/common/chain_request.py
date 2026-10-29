"""The shared envelope every chain request carries.

All five request builders (three DEX builders plus the two swap builders in the
dispatcher) used to spell out the same seven fields, and they each re-implemented
the same slippage clamp with a slightly different expression. That is the kind of
duplication where one copy is eventually edited and the others are not — and the
field in question is the one that decides how much price movement a signed
transaction will accept.

``base_request`` produces the envelope only. Each DEX builder adds its own
action-specific fields, because those genuinely differ (bin ranges and bps are
Meteora/CLMM concepts, mint pairs are Jupiter's).
"""

from typing import Any, Dict

DEFAULT_MAX_SLIPPAGE_BPS = 100
DEFAULT_PRIORITY_FEE_CAP_SOL = 0.0005
DEFAULT_FALLBACK_DELAY_SECONDS = 15


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def slippage_bps(signal: Dict[str, Any], rails: Dict[str, Any]) -> int:
    """Requested slippage, clamped to the rail.

    A signal may ask for less than the rail allows; it may never ask for more.
    A non-numeric request falls back to the rail ceiling rather than to zero.
    """
    rail = _as_int(rails.get("max_slippage_bps"), DEFAULT_MAX_SLIPPAGE_BPS)
    requested = _as_int(signal.get("max_slippage_bps"), rail)
    return min(requested, rail)


def base_request(dex: str, signal: Dict[str, Any], cfg: Dict[str, Any],
                 rails: Dict[str, Any], *, action: str = None) -> Dict[str, Any]:
    """Common envelope for a chain request (mode, rpc, wallet, fee caps)."""
    req: Dict[str, Any] = {
        "dex": dex,
        "mode": "send",
        "rpc_url": cfg["rpc_https_url"],
        "fallback_rpc_urls": cfg.get("rpc_fallback_urls", []),
        "fallback_delay_seconds": cfg.get("rpc_fallback_delay_seconds",
                                          DEFAULT_FALLBACK_DELAY_SECONDS),
        "wallet_public_key": cfg["wallet_public_key"],
        "wallet_id": signal.get("wallet_id") or "main",
        "max_slippage_bps": slippage_bps(signal, rails),
        "priority_fee_cap_sol": rails.get("priority_fee_cap_sol",
                                          DEFAULT_PRIORITY_FEE_CAP_SOL),
    }
    if action is not None:
        req["action"] = action
    return req


def claim_action(action: str) -> str:
    """Map claim_fees/claim_rewards onto the chain layer's single 'claim'."""
    return "claim" if action in {"claim_fees", "claim_rewards"} else action
