"""Shared signal validation for the multi-DEX executor."""

import base64
import json
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common.solana import DEX_PROGRAM_IDS as _PROGRAM_IDS


# --------------------------------------------------------------------------
# In-wake cache for pool state (populated by dispatcher.run_once())
# --------------------------------------------------------------------------
_pool_tick_cache: Dict[tuple, Optional[int]] = {}


def _cached_pool_tick(dex: str, pool_address: str, rpc_url: str) -> Optional[int]:
    """Return current tick from cache, or None (populate cache miss).

    The cache is per-signal, not per-process: the dispatcher clears it through
    ``clear_pool_caches()`` before each signal (same boundary as the preflight
    caches). Without that, a long-running dispatcher would keep validating
    opens against a tick read hours earlier.
    """
    key = (dex, pool_address, rpc_url)
    if key in _pool_tick_cache:
        return _pool_tick_cache[key]
    # fetch_pool_current_tick lives in this module (defined below); the
    # module-global lookup resolves at call time, so tests patching
    # common.signal_validator.fetch_pool_current_tick keep working.
    result = fetch_pool_current_tick(dex, pool_address, rpc_url)
    _pool_tick_cache[key] = result
    return result


def clear_pool_caches() -> None:
    """Drop the in-wake pool-tick cache (called once per processed signal)."""
    _pool_tick_cache.clear()


SUPPORTED_DEXES = {"meteora", "raydium", "orca"}
REQUIRED_FIELDS = {"signal_id", "action", "created_at"}
SWAP_REQUIRED_FIELDS = {"input_mint", "output_mint", "amount"}
SWAP_TO_USDC_REQUIRED_FIELDS = {"mint", "symbol", "decimals", "amount_raw", "amount_ui", "value_usd", "reason"}
VALID_ACTIONS = {"open", "close", "claim_fees", "claim_rewards", "swap", "swap_to_usdc", "add_liquidity", "remove_liquidity", "rebalance", "rotate"}

# Byte offset of the live current tick/bin (i32, little-endian, signed) in
# each DEX's on-chain pool account. All three verified against live mainnet
# accounts; Raydium verified 2026-09-30 on 3 mainnet CLMM pools (spacing
# u16@235 matched 120/60/60 and tickCurrent@269 equals the value implied by
# sqrtPriceX64@253 on every account — the old 304 derivation was wrong).
_TICK_DECODE_OFFSETS = {
    "meteora": 76,   # LbPair.activeId
    "orca": 81,      # Whirlpool.tickCurrentIndex
    "raydium": 269,  # PoolState.tickCurrent
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

# Narrowest range width allowed per DEX when no rail key is configured. These
# are the owner's values from execution_limits.json, so a missing key can only
# narrow (never widen) the rail.
_RANGE_WIDTH_DEFAULT = 2000
_RANGE_WIDTH_DEFAULTS = {"meteora": 70, "orca": 2000, "raydium": 2000}


# Owning program of each pool account. Decoding bytes from an account that is
# not owned by the expected DEX program can yield a plausible-looking i32/u16,
# which would let a wrong-account read pass the tick / bin-step rails. The
# table is shared with the preflight path (common/solana.py) so the two readers
# cannot disagree about which program owns a pool.



class SignalValidationError(Exception):
    pass


def _fetch_pool_account_blob(pool_address: str, rpc_url: str,
                             timeout: float,
                             expected_program: Optional[str] = None) -> Optional[bytes]:
    """Return the raw account bytes, or None when unavailable.

    ``expected_program`` fails the read closed when the account is owned by a
    different program (or when the RPC omits the owner): a wrong-layout decode
    must never be mistaken for a market read.
    """
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
        if expected_program is not None and value.get("owner") != expected_program:
            return None
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
    dex_key = (dex or "").lower()
    offset = _TICK_DECODE_OFFSETS.get(dex_key)
    if not offset:
        return None
    blob = _fetch_pool_account_blob(pool_address, rpc_url, timeout,
                                    _PROGRAM_IDS.get(dex_key))
    if blob is None or len(blob) < offset + 4:
        return None
    return int.from_bytes(blob[offset : offset + 4], "little", signed=True)


def fetch_pool_bin_step(dex: str, pool_address: str, rpc_url: str,
                        timeout: float = 10.0) -> Optional[int]:
    """Return the pool's on-chain bin/tick step (u16), or None when unavailable."""
    dex_key = (dex or "").lower()
    offset = _BIN_STEP_DECODE_OFFSETS.get(dex_key)
    if not offset:
        return None
    blob = _fetch_pool_account_blob(pool_address, rpc_url, timeout,
                                    _PROGRAM_IDS.get(dex_key))
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


def _check_side(signal: Dict[str, Any], action: str) -> None:
    """Validate side declaration and liquidity consistency for mutations.

    Accepts missing side for backwards compatibility (assumed bidirectional).
    For explicit one-sided positions the supplied liquidity must match the
    chosen side; a remove on the X side with non-zero Y bps, or vice versa,
    is rejected because the chain instruction would act on the wrong token.
    """
    side = (signal.get("side") or "bidirectional").lower()
    if side not in {"bidirectional", "x", "y"}:
        raise SignalValidationError(
            f"{action} side must be bidirectional, x, or y; got {side}"
        )
    liq = signal.get("liquidity") or {}
    try:
        amount_x = int(liq.get("amount_x", 0))
    except (TypeError, ValueError):
        amount_x = 0
    try:
        amount_y = int(liq.get("amount_y", 0))
    except (TypeError, ValueError):
        amount_y = 0
    if side == "x" and amount_y != 0:
        raise SignalValidationError(
            f"{action} side=x but liquidity.amount_y is non-zero"
        )
    if side == "y" and amount_x != 0:
        raise SignalValidationError(
            f"{action} side=y but liquidity.amount_x is non-zero"
        )
    # For removes the bps applies to the chosen side; ensure one side is
    # explicitly zero when side is x/y.
    if action == "remove_liquidity":
        if side == "x" and amount_x != 0:
            raise SignalValidationError(
                "remove_liquidity side=x but liquidity.amount_x is non-zero"
            )
        if side == "y" and amount_y != 0:
            raise SignalValidationError(
                "remove_liquidity side=y but liquidity.amount_y is non-zero"
            )


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


def signal_age_seconds(signal: Dict[str, Any]) -> Optional[float]:
    """Age of a signal in seconds, or None when created_at is unusable.

    A naive timestamp is read as UTC: the signals in circulation carry an
    explicit offset, but a bare timestamp must not crash the rail.
    """
    created_at = signal.get("created_at")
    if not isinstance(created_at, str):
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def _check_freshness(signal: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    age = signal_age_seconds(signal)
    if age is None:
        raise SignalValidationError(
            f"invalid created_at: {signal.get('created_at')}")
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
        elif action == "add_liquidity":
            required |= {"position_id", "bin_range", "liquidity", "position_usd"}
        elif action == "remove_liquidity":
            required |= {"position_id", "bps"}
        elif action == "rebalance":
            required |= {"position_id", "bin_range", "liquidity", "position_usd"}
        elif action == "rotate":
            required |= {"position_id", "bin_range", "liquidity", "position_usd"}

    missing = required - set(signal.keys())
    if missing:
        raise SignalValidationError(f"missing fields: {sorted(missing)}")

    if action == "swap":
        _check_swap(signal, cfg, rails)
        _check_freshness(signal, cfg)
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

    if action in {"open", "add_liquidity", "rebalance", "rotate"}:
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

        # Range-width rail, per DEX. This check used to be implemented three
        # times (once per dex validator, plus a second time for Meteora
        # mutations), which is how the per-DEX values drifted apart.
        #
        # Every cap that applies is enforced and the tightest wins, so a
        # missing per-DEX key can never widen the rail: Meteora's floor is 70
        # bins, the CLMMs' is 2000, and a global max_bin_range_width only ever
        # narrows that further.
        max_width = min(
            _RANGE_WIDTH_DEFAULTS.get(dex, _RANGE_WIDTH_DEFAULT),
            int(rails.get("max_bin_range_width", _RANGE_WIDTH_DEFAULT)),
            int(rails.get(f"max_{dex}_range_width",
                          _RANGE_WIDTH_DEFAULTS.get(dex, _RANGE_WIDTH_DEFAULT))),
        )
        width = upper - lower + 1
        if width > max_width:
            raise SignalValidationError(
                f"{dex} range width {width} (inclusive) exceeds rail {max_width}")

        # Live-price guard: a range that does not contain the pool's current
        # tick/bin would deposit one-sided at a stale price (the ZEC/USDC
        # center=0 bug). add_liquidity shares the guard: adding into a range
        # the price has left parks the new capital in dead bins. The chain
        # handler separately asserts the supplied range equals the position's
        # on-chain bounds, so a stale signal cannot add to the wrong range.
        # Only enforced when the tick can be fetched; a fetch failure warns
        # instead of blocking execution.
        tick = _cached_pool_tick(
            dex, signal.get("pool_address"), cfg.get("rpc_https_url")
        )
        if tick is None:
            print(
                f"[validator] WARN: current tick unavailable for {action} "
                f"{signal.get('signal_id')} (dex={dex}); tick guard skipped",
                file=sys.stderr,
            )
        elif not (lower <= tick <= upper):
            raise SignalValidationError(
                f"{action} range [{lower}, {upper}] does not contain pool "
                f"current tick {tick} (dex={dex}); refusing to proceed off "
                "the live price"
            )

    if action in {"close", "claim_fees", "claim_rewards", "add_liquidity", "remove_liquidity", "rebalance", "rotate"} and not signal.get("position_id"):
        raise SignalValidationError(f"{action} requires position_id")

    if action in {"add_liquidity", "rebalance", "rotate"}:
        liq = signal.get("liquidity") or {}
        amounts: List[int] = []
        for key in ("amount_x", "amount_y"):
            try:
                value = int(liq.get(key, 0))
            except (TypeError, ValueError) as exc:
                raise SignalValidationError(
                    f"add_liquidity liquidity.{key} must be an integer: {liq.get(key)}"
                ) from exc
            if value < 0:
                raise SignalValidationError(
                    f"add_liquidity liquidity.{key} must be non-negative: {value}"
                )
            amounts.append(value)
        if all(v == 0 for v in amounts):
            raise SignalValidationError(
                f"{action} liquidity must include a positive amount_x or amount_y"
            )

        try:
            add_usd = float(signal.get("position_usd"))
        except (TypeError, ValueError) as exc:
            raise SignalValidationError(
                f"{action} signal missing position_usd; cannot verify exposure rails"
            ) from exc
        if add_usd <= 0:
            raise SignalValidationError(
                f"{action} position_usd must be positive: {add_usd}"
            )

    # Side discipline: mutations of asymmetric positions must declare the
    # side they target, and the supplied liquidity must be consistent with
    # that side. Missing side on a mutation is accepted (legacy / symmetric).
    if action in {"add_liquidity", "remove_liquidity", "rebalance", "rotate"}:
        _check_side(signal, action)

    if action == "remove_liquidity":
        try:
            bps = int(signal.get("bps"))
        except (TypeError, ValueError) as exc:
            raise SignalValidationError(
                f"remove_liquidity bps must be an integer: {signal.get('bps')}"
            ) from exc
        min_remove_bps = int(rails.get("min_remove_bps", 1))
        if bps < min_remove_bps:
            raise SignalValidationError(
                f"remove_liquidity bps {bps} < min_remove_bps {min_remove_bps}"
            )
        if bps >= 10000:
            raise SignalValidationError(
                "remove_liquidity bps must be < 10000; a full removal is close's job "
                "(remove+claim+close atomically)"
            )
        partial_min = int(rails.get("partial_remove_bps_min", 2500))
        partial_max = int(rails.get("partial_remove_bps_max", 7500))
        if bps < partial_min or bps > partial_max:
            raise SignalValidationError(
                f"remove_liquidity bps {bps} outside partial-remove range "
                f"[{partial_min}, {partial_max}]"
            )

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

        # Score floor: Sheldon owns the threshold value, George enforces it
        # fail-closed. An open without a score cannot prove it crossed the
        # threshold, so it is rejected (owner-confirmed policy).
        # Adaptive signals carry the threshold used by the producer;
        # clamp to the executor's adaptive floor so a missing/buggy signal
        # cannot open below a safe minimum.
        adaptive_floor = float(rails.get("min_open_score_adaptive_floor", 55.0))
        score_policy = signal.get("score_policy") or {}
        threshold = score_policy.get("min_open_score")
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            threshold = float(rails.get("min_open_score", 70.0))
        threshold = max(threshold, adaptive_floor)
        try:
            score = float(signal.get("score"))
        except (TypeError, ValueError):
            raise SignalValidationError(
                "open signal missing score; cannot verify min_open_score rail"
            ) from None
        if score < threshold:
            raise SignalValidationError(
                f"score {score} < min_open_score {threshold} "
                f"(adaptive_floor={adaptive_floor})"
            )

    return signal


__all__ = [
    "SignalValidationError",
    "signal_age_seconds",
    "validate_core",
]
