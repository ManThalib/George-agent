"""Validate incoming signal files against the executor schema and rails."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from common import load_json


SUPPORTED_DEXES = {"meteora", "raydium", "orca"}
REQUIRED_FIELDS = {"signal_id", "action", "created_at"}
SWAP_REQUIRED_FIELDS = {"input_mint", "output_mint", "amount"}
# Fields emitted by Sheldon's scoring agent for a dust swap_to_usdc signal.
SWAP_TO_USDC_REQUIRED_FIELDS = {"mint", "symbol", "decimals", "amount_raw", "amount_ui", "value_usd", "reason"}
VALID_ACTIONS = {"open", "close", "claim_fees", "claim_rewards", "swap", "swap_to_usdc"}


class SignalValidationError(Exception):
    pass


def _validate_core(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    action = signal.get("action")
    if action not in VALID_ACTIONS:
        raise SignalValidationError(f"invalid action: {action}")

    required = set(REQUIRED_FIELDS)
    if action == "swap":
        required |= set(SWAP_REQUIRED_FIELDS)
    elif action == "swap_to_usdc":
        required |= set(SWAP_TO_USDC_REQUIRED_FIELDS)
    else:
        required |= {"pool_address", "bin_range", "liquidity"}

    missing = required - set(signal.keys())
    if missing:
        raise SignalValidationError(f"missing fields: {sorted(missing)}")

    if action == "swap":
        return _validate_swap(signal, cfg, rails)
    if action == "swap_to_usdc":
        return _validate_swap_to_usdc(signal, cfg, rails)

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

    # Width guardrail.  Sheldon's adaptive ranges can now span up to
    # 1000 bins per side (2000 total).  Meteora uses bin IDs here; Raydium
    # and Orca use ticks, so allow per-DEX overrides while sharing the
    # same default cap.
    width = upper - lower
    max_width = _max_range_width(dex, rails)
    if width > max_width:
        raise SignalValidationError(
            f"{dex} range width {width} exceeds rail {max_width}"
        )

    # close/claim actions require position_id.
    if action in {"close", "claim_fees", "claim_rewards"} and not signal.get("position_id"):
        raise SignalValidationError(f"{action} requires position_id")

    return signal


def _max_range_width(dex: str, rails: Dict[str, Any]) -> int:
    """Return the allowed range width for the given DEX.

    Prefer an explicit `max_{dex}_range_width` rail; otherwise use the
    shared `max_bin_range_width` rail.  Default is generous enough for
    Sheldon's adaptive half-width of 1000 bins per side (2000 total).
    """
    explicit = rails.get(f"max_{dex}_range_width")
    if explicit is not None:
        return int(explicit)
    return int(rails.get("max_bin_range_width", 2000))


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


def _validate_swap_to_usdc(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a swap_to_usdc dust-swap signal from Sheldon.

    These signals are never executed automatically; they are queued for
    manual review.  Validation only ensures the payload is well-formed.
    """
    mint = signal.get("mint")
    if not isinstance(mint, str) or len(mint) < 32:
        raise SignalValidationError("mint must be a base58 Solana mint address")

    symbol = signal.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise SignalValidationError("symbol must be a non-empty string")

    decimals = signal.get("decimals")
    if isinstance(decimals, str):
        try:
            decimals = int(decimals)
        except (TypeError, ValueError) as exc:
            raise SignalValidationError(f"decimals must be an integer: {decimals}") from exc
    if not isinstance(decimals, int) or decimals < 0 or decimals > 255:
        raise SignalValidationError("decimals must be an integer between 0 and 255")

    for field in ("amount_raw",):
        try:
            value = int(signal[field])
        except (TypeError, ValueError) as exc:
            raise SignalValidationError(f"{field} must be a positive integer: {signal.get(field)}") from exc
        if value <= 0:
            raise SignalValidationError(f"{field} must be positive: {value}")

    for field in ("amount_ui", "value_usd"):
        try:
            value = float(signal[field])
        except (TypeError, ValueError) as exc:
            raise SignalValidationError(f"{field} must be numeric: {signal.get(field)}") from exc
        if value < 0:
            raise SignalValidationError(f"{field} must be non-negative: {value}")

    reason = signal.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise SignalValidationError("reason must be a non-empty string")

    # Freshness check still applies even though execution is manual.
    try:
        created = datetime.fromisoformat(signal["created_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - created).total_seconds()
    except (ValueError, AttributeError) as exc:
        raise SignalValidationError(f"invalid created_at: {signal.get('created_at')}") from exc
    max_age = cfg.get("signal_max_age_seconds", 300)
    if age > max_age:
        raise SignalValidationError(f"signal stale: {age:.0f}s > {max_age}s")

    return signal


def validate(signal_path: Path, cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a signal file. Raises SignalValidationError on any failure."""
    signal = load_json(signal_path)
    return _validate_core(signal, cfg, rails)


def validate_dict(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an in-memory signal dict."""
    return _validate_core(signal, cfg, rails)
