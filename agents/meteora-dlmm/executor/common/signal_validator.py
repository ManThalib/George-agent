"""Shared signal validation for the multi-DEX executor."""

import base64
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from common.utils import load_json


SUPPORTED_DEXES = {"meteora", "raydium", "orca"}
REQUIRED_FIELDS = {"signal_id", "action", "created_at"}
SWAP_REQUIRED_FIELDS = {"input_mint", "output_mint", "amount"}
SWAP_TO_USDC_REQUIRED_FIELDS = {"mint", "symbol", "decimals", "amount_raw", "amount_ui", "value_usd", "reason"}
VALID_ACTIONS = {"open", "close", "claim_fees", "claim_rewards", "swap", "swap_to_usdc"}

# Byte offset of the live current tick/bin (i32, little-endian, signed) in
# each DEX's on-chain pool account. Meteora and Orca verified against live
# mainnet accounts; Raydium derived from the zero-copy POD layout and must
# be treated as best-effort.
_TICK_DECODE_OFFSETS = {
    "meteora": 76,   # LbPair.activeId
    "orca": 81,      # Whirlpool.tickCurrentIndex
    "raydium": 304,  # PoolState.tickCurrent
}

# Byte offset of the pool's bin/tick step (u16, little-endian, unsigned).
# Only Meteora DLMM appears here: bins are a DLMM concept, Orca/Raydium
# CLMMs use tick_spacing instead.
#   meteora: LbPair.bin_step @ 80, immediately after LbPair.activeId (i32 @ 76).
#     Verified on three live mainnet LbPairs with reported bin_steps 4, 10,
#     and 20: the u16 at offset 80 matched the reported value on every
#     account. (Offset 73 also echoes the value — that is the bin_step_seed
#     PDA copy, not the typed field; do not use it.)
_BIN_STEP_DECODE_OFFSETS = {
    "meteora": 80,
}


class SignalValidationError(Exception):
    pass


def _fetch_pool_account_blob(pool_address: str, rpc_url: str,
                             timeout: float) -> Optional[bytes]:
    """Return the raw account bytes, or None when unavailable."""
    if not pool_address or not rpc_url:
        return None
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [pool_address,
                       {"encoding": "base64", "commitment": "confirmed"}],
        }
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        value = (result or {}).get("result", {}).get("value") or {}
        data = value.get("data")
        raw = data[0] if isinstance(data, list) and data else data
        if not isinstance(raw, str):
            return None
        return base64.b64decode(raw)
    except Exception:
        return None


def fetch_pool_current_tick(dex: str, pool_address: str, rpc_url: str,
                            timeout: float = 10.0) -> Optional[int]:
    """Return the pool's live current tick/bin, or None when unavailable."""
    offset = _TICK_DECODE_OFFSETS.get((dex or "").lower())
    if not offset:
        return None
    blob = _fetch_pool_account_blob(pool_address, rpc_url, timeout)
    if blob is None or len(blob) < offset + 4:
        return None
    return int.from_bytes(blob[offset : offset + 4], "little", signed=True)


def fetch_pool_bin_step(dex: str, pool_address: str, rpc_url: str,
                        timeout: float = 10.0) -> Optional[int]:
    """Return the pool's on-chain bin/tick step (u16), or None when unavailable."""
    offset = _BIN_STEP_DECODE_OFFSETS.get((dex or "").lower())
    if not offset:
        return None
    blob = _fetch_pool_account_blob(pool_address, rpc_url, timeout)
    if blob is None or len(blob) < offset + 2:
        return None
    return int.from_bytes(blob[offset : offset + 2], "little", signed=False)


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

        # Live-price guard: an open range that does not contain the pool's
        # current tick/bin opens one-sided at a stale price (the ZEC/USDC
        # center=0 bug). Only enforced when the tick can be fetched; a
        # fetch failure warns instead of blocking execution.
        tick = fetch_pool_current_tick(
            dex, signal.get("pool_address"), cfg.get("rpc_https_url")
        )
        if tick is None:
            print(
                f"[validator] WARN: current tick unavailable for open "
                f"{signal.get('signal_id')} (dex={dex}); tick guard skipped",
                file=sys.stderr,
            )
        elif not (lower <= tick <= upper):
            raise SignalValidationError(
                f"open range [{lower}, {upper}] does not contain pool current "
                f"tick {tick} (dex={dex}); refusing to open a range off the "
                "live price"
            )

    if action in {"close", "claim_fees", "claim_rewards"} and not signal.get("position_id"):
        raise SignalValidationError(f"{action} requires position_id")

    # Position sizing rails: Sheldon owns the policy, but George enforces it
    # fail-closed before any transaction can reach the builder.
    if action == "open":
        position_usd = signal.get("position_usd")
        try:
            position_usd = float(position_usd)
        except (TypeError, ValueError):
            raise SignalValidationError(
                "open signal missing position_usd; cannot verify sizing rails"
            ) from None
        min_pos = float(rails.get("min_position_usd", 10.0))
        max_pos = float(rails.get("max_position_usd", 100.0))
        if position_usd < min_pos:
            raise SignalValidationError(
                f"position_usd {position_usd:.2f} < min_position_usd {min_pos:.2f}"
            )
        if position_usd > max_pos:
            raise SignalValidationError(
                f"position_usd {position_usd:.2f} > max_position_usd {max_pos:.2f}"
            )

    return signal


def validate(signal_path: Path, cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a signal file."""
    signal = load_json(signal_path)
    return validate_core(signal, cfg, rails)
