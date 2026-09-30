"""Meteora DLMM signal validator."""

from typing import Any, Dict, List

from common import signal_validator
from common.signal_validator import SignalValidationError, validate_core


def _allowed_bin_steps(rails: Dict[str, Any]) -> List[int]:
    """Return the allowed_bin_steps rail as a sorted int list.

    Falls back to the SAFETY_RAILS.md default when the rail is missing or
    malformed — a broken rail must never widen the whitelist.
    """
    raw = rails.get("allowed_bin_steps")
    if not isinstance(raw, (list, tuple)) or not raw:
        raw = [10, 20, 25, 50, 100]
    try:
        return sorted({int(step) for step in raw})
    except (TypeError, ValueError):
        return [10, 20, 25, 50, 100]


def validate(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    signal = validate_core(signal, cfg, rails)

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
            "allowed_bin_steps rail unverified"
        )
    allowed = _allowed_bin_steps(rails)
    if bin_step not in allowed:
        raise SignalValidationError(
            f"pool bin_step {bin_step} not in allowed_bin_steps {allowed} "
            f"(minimum {min(allowed)}); refusing to open"
        )

    return signal
