"""Validate incoming signal files against the executor schema and rails."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from common import load_json


SUPPORTED_DEXES = {"meteora", "raydium", "orca"}
REQUIRED_FIELDS = {"signal_id", "action", "created_at"}
SWAP_REQUIRED_FIELDS = {"input_mint", "output_mint", "amount"}
VALID_ACTIONS = {"open", "close", "claim_fees", "claim_rewards", "swap"}


class SignalValidationError(Exception):
    pass


def _validate_core(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    action = signal.get("action")
    if action not in VALID_ACTIONS:
        raise SignalValidationError(f"invalid action: {action}")

    required = set(REQUIRED_FIELDS)
    if action == "swap":
        required |= set(SWAP_REQUIRED_FIELDS)
    else:
        required |= {"pool_address", "bin_range", "liquidity"}

    missing = required - set(signal.keys())
    if missing:
        raise SignalValidationError(f"missing fields: {sorted(missing)}")

    if action == "swap":
        return _validate_swap(signal, cfg, rails)

    dex = signal.get("dex") or "meteora"
    if dex not in SUPPORTED_DEXES:
        raise SignalValidationError(f"unsupported dex: {dex}")

    # Stale signal check
    try:
        created = datetime.fromisoformat(signal["created_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - created).total_seconds()
    except (ValueError, AttributeError) as exc:
        raise SignalValidationError(f"invalid created_at: {signal.get('created_at')}") from exc
    max_age = cfg.get("signal_max_age_seconds", 300)
    if age > max_age:
        raise SignalValidationError(f"signal stale: {age:.0f}s > {max_age}s")

    # Slippage rail: signal may only be tighter than the rail.
    slippage_bps = int(signal.get("max_slippage_bps", 0))
    rail_slippage = int(rails.get("max_slippage_bps", 100))
    if slippage_bps > rail_slippage:
        raise SignalValidationError(f"max_slippage_bps {slippage_bps} > rail {rail_slippage}")

    # Per-DEX bin/tick sanity checks.
    bin_range = signal.get("bin_range") or {}
    lower = bin_range.get("lower")
    upper = bin_range.get("upper")
    if lower is None or upper is None:
        raise SignalValidationError("bin_range.lower and bin_range.upper are required")
    try:
        lower = int(lower)
        upper = int(upper)
    except (TypeError, ValueError) as exc:
        raise SignalValidationError(f"bin_range values must be integers: {exc}") from exc
    if lower > upper:
        raise SignalValidationError(f"bin_range.lower ({lower}) > upper ({upper})")

    if dex == "meteora":
        if lower < 0 or upper < 0:
            raise SignalValidationError(f"meteora bin IDs must be non-negative: {lower}..{upper}")
    else:
        # Raydium / Orca use signed ticks; only check ordering.
        pass

    # close/claim actions require position_id.
    if action in {"close", "claim_fees", "claim_rewards"} and not signal.get("position_id"):
        raise SignalValidationError(f"{action} requires position_id")

    return signal


def _validate_swap(signal, cfg, rails):
    """Validate a swap action signal."""
    try:
        amount = int(signal["amount"])
    except (ValueError, TypeError) as exc:
        raise SignalValidationError(f"swap amount must be an integer string: {signal.get('amount')}") from exc
    if amount <= 0:
        raise SignalValidationError(f"swap amount must be positive: {amount}")

    slippage_bps = int(signal.get("max_slippage_bps", 0))
    rail_slippage = int(rails.get("max_slippage_bps", 100))
    if slippage_bps > rail_slippage:
        raise SignalValidationError(f"max_slippage_bps {slippage_bps} > rail {rail_slippage}")

    for mint, field in [(signal.get("input_mint"), "input_mint"), (signal.get("output_mint"), "output_mint")]:
        if not isinstance(mint, str) or len(mint) < 32:
            raise SignalValidationError(f"{field} must be a base58 Solana mint address")

    return signal


def validate(signal_path: Path, cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a signal file. Raises SignalValidationError on any failure."""
    signal = load_json(signal_path)
    return _validate_core(signal, cfg, rails)


def validate_dict(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an in-memory signal dict."""
    return _validate_core(signal, cfg, rails)
