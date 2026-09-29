"""Shared signal validation for the multi-DEX executor."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from common.utils import load_json


SUPPORTED_DEXES = {"meteora", "raydium", "orca"}
REQUIRED_FIELDS = {"signal_id", "action", "created_at"}
SWAP_REQUIRED_FIELDS = {"input_mint", "output_mint", "amount"}
SWAP_TO_USDC_REQUIRED_FIELDS = {"mint", "symbol", "decimals", "amount_raw", "amount_ui", "value_usd", "reason"}
VALID_ACTIONS = {"open", "close", "claim_fees", "claim_rewards", "swap", "swap_to_usdc"}


class SignalValidationError(Exception):
    pass


def _check_swap(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> None:
    amount = signal.get("amount")
    try:
        amount = int(amount)
    except (TypeError, ValueError) as exc:
        raise SignalValidationError(f"swap amount must be a positive integer: {signal.get('amount')}") from exc
    if amount <= 0:
        raise SignalValidationError(f"swap amount must be positive: {amount}")
    for field in ("input_mint", "output_mint"):
        mint = signal.get(field)
        if not isinstance(mint, str) or len(mint) < 32:
            raise SignalValidationError(f"{field} must be a base58 Solana mint address")
    slippage_bps = int(signal.get("max_slippage_bps", 0))
    rail_slippage = int(rails.get("max_slippage_bps", 100))
    if slippage_bps > rail_slippage:
        raise SignalValidationError(f"max_slippage_bps {slippage_bps} > rail {rail_slippage}")


def _check_swap_to_usdc(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> None:
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


def _check_freshness(signal: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    try:
        created = datetime.fromisoformat(signal["created_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - created).total_seconds()
    except (ValueError, AttributeError) as exc:
        raise SignalValidationError(f"invalid created_at: {signal.get('created_at')}") from exc
    max_age = cfg.get("signal_max_age_seconds", 300)
    if age > max_age:
        raise SignalValidationError(f"signal stale: {age:.0f}s > {max_age}s")


def validate_core(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an in-memory signal dict. Raises SignalValidationError."""
    action = signal.get("action")
    if action not in VALID_ACTIONS:
        raise SignalValidationError(f"invalid action: {action}")

    required = set(REQUIRED_FIELDS)
    if action == "swap":
        required |= set(SWAP_REQUIRED_FIELDS)
    elif action == "swap_to_usdc":
        required |= set(SWAP_TO_USDC_REQUIRED_FIELDS)
    else:
        required |= {"pool_address"}
        if action == "open":
            required |= {"bin_range", "liquidity"}

    missing = required - set(signal.keys())
    if missing:
        raise SignalValidationError(f"missing fields: {sorted(missing)}")

    if action == "swap":
        _check_swap(signal, cfg, rails)
        return signal
    if action == "swap_to_usdc":
        _check_swap_to_usdc(signal, cfg, rails)
        _check_freshness(signal, cfg)
        return signal

    dex = signal.get("dex") or "meteora"
    if dex not in SUPPORTED_DEXES:
        raise SignalValidationError(f"unsupported dex: {dex}")

    _check_freshness(signal, cfg)

    slippage_bps = int(signal.get("max_slippage_bps", 0))
    rail_slippage = int(rails.get("max_slippage_bps", 100))
    if slippage_bps > rail_slippage:
        raise SignalValidationError(f"max_slippage_bps {slippage_bps} > rail {rail_slippage}")

    if action == "open":
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

    if action in {"close", "claim_fees", "claim_rewards"} and not signal.get("position_id"):
        raise SignalValidationError(f"{action} requires position_id")

    return signal


def validate(signal_path: Path, cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a signal file."""
    signal = load_json(signal_path)
    return validate_core(signal, cfg, rails)
