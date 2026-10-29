"""Meteora DLMM signal validator.

Common rules (required fields, tick containment, range width, remove bps,
sizing, score floor) live in common.signal_validator; this module adds the one
Meteora-specific rail that cannot be expressed generically: the on-chain
bin_step must be inside the owner's bin_step_rule.
"""

from typing import Any, Dict

from common import signal_validator
from common.bin_step_rail import (
    DEFAULT_BIN_STEP_RULE,
    meteora_bin_step_allowed,
    normalize_rule,
)
from common.signal_validator import SignalValidationError, validate_core


def _bin_step_rule(rails: Dict[str, Any]) -> Dict[str, int]:
    """Return the bin_step rule rail, defaulting to the owner rule.

    A missing or malformed rail falls back to the default rule — never to a
    widened one.
    """
    return normalize_rule(rails.get("bin_step_rule", DEFAULT_BIN_STEP_RULE))


def validate(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    signal = validate_core(signal, cfg, rails)

    if signal.get("action") != "open":
        return signal

    # Bin-step rail (SAFETY_RAILS.md `bin_step_rule`). The step is read from
    # the pool's live on-chain account (u16 @ 80), never from
    # producer-supplied signal metadata: the executor must not trust the
    # producer to police itself. Fail-closed on fetch failure — the open
    # needs RPC to execute anyway, and a rail that disappears when the
    # fetch fails is not a rail.
    bin_step = signal_validator.fetch_pool_bin_step(
        "meteora", signal.get("pool_address"), cfg.get("rpc_https_url")
    )
    if bin_step is None:
        raise SignalValidationError(
            f"cannot verify pool bin_step for {signal.get('pool_address')} "
            "(on-chain fetch failed); refusing to open with the "
            "bin_step_rule rail unverified"
        )
    ok, why = meteora_bin_step_allowed(bin_step, _bin_step_rule(rails))
    if not ok:
        raise SignalValidationError(f"pool {why}; refusing to open")

    return signal
