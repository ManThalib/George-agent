"""Meteora DLMM signal validator."""

from typing import Any, Dict, List

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

    if signal.get("action") in {"add_liquidity", "remove_liquidity", "rebalance", "rotate"}:
        return validate_mutation(signal, cfg, rails)

    if signal.get("action") != "open":
        return signal

    bin_range = signal.get("bin_range") or {}
    lower = int(bin_range.get("lower", 0))
    upper = int(bin_range.get("upper", 0))
    width = upper - lower + 1
    max_width = int(rails.get("max_meteora_range_width", 70))
    if width > max_width:
        raise SignalValidationError(f"meteora range width {width} (inclusive) exceeds rail {max_width}")

    # Bin-step rail (SAFETY_RAILS.md `allowed_bin_steps`). The step is read
    # from the pool's live on-chain account (u16 @ 80), never from
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
    _ok, _why = meteora_bin_step_allowed(bin_step, _bin_step_rule(rails))
    if not _ok:
        raise SignalValidationError(
            f"pool {_why}; refusing to open"
        )

    return signal


def validate_mutation(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Per-DEX checks for add_liquidity / remove_liquidity on Meteora.

    Runs after common validate_core (which enforces required fields, the
    remove bps rail, and the add live-tick guard).
    """
    action = signal.get("action")
    if action not in {"add_liquidity", "remove_liquidity", "rebalance", "rotate"}:
        return signal

    if action in {"add_liquidity", "rebalance", "rotate"}:
        bin_range = signal.get("bin_range") or {}
        lower = int(bin_range.get("lower", 0))
        upper = int(bin_range.get("upper", 0))
        width = upper - lower + 1
        max_width = int(rails.get("max_meteora_range_width", 70))
        if width > max_width:
            raise SignalValidationError(
                f"meteora add range width {width} (inclusive) exceeds rail {max_width}"
            )

    return signal
