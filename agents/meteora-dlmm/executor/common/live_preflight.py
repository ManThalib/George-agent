"""Live balance preflight for opens (George update plan 03, Change B-D).

Immediately before an `open` is simulated, George re-checks the wallet's
*live* state instead of trusting the signal (which may come from a stale
Missy scan, an unsettled prep swap, or a duplicate pending open):

  * native SOL: rent for the position/bin-array accounts plus transaction
    fees plus the SOL side of a SOL-paired position;
  * token X / token Y: the raw amounts the position will deposit.

A shortfall is reported mechanically (``INSUFFICIENT_SOL_FOR_OPEN`` /
``INSUFFICIENT_TOKEN_BALANCE``) instead of relying on the raw Solana
``insufficient funds`` error surfacing from a simulation or send.

The requirement is DEX-specific (per-DEX account/reference overhead),
signal-specific (liquidity, pool, wallet), position-size-specific (the
required raw amounts scale with the signal's liquidity), SOL-pair-aware
(a WSOL side is funded from native SOL, not checked as an SPL balance) and
grounded in live account state (getBalance / getTokenAccountsByOwner plus
the pool's own on-chain mints).

Fail-closed: if the live state cannot be read the open is rejected rather
than attempted.
"""

import base64
import json
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

# --------------------------------------------------------------------------
# In-wake caches for pool state (populated by dispatcher.run_once())
# --------------------------------------------------------------------------
_pool_mints_cache: Dict[tuple, Optional[tuple]] = {}
_pool_tick_cache: Dict[tuple, Optional[int]] = {}

# --------------------------------------------------------------------------
# In-wake caches for wallet balances (populated by dispatcher.run_once())
# --------------------------------------------------------------------------
_wallet_sol_cache: Dict[tuple, int] = {}
_wallet_token_cache: Dict[tuple, Dict[str, int]] = {}


def clear_preflight_caches() -> None:
    """Drop all in-wake caches.

    Called by the dispatcher after each processed signal: a mid-wake swap
    changes real SOL/USDC balances, so a later open in the same wake must
    re-read live state, not spend a previous leg's balances twice.
    """
    _pool_mints_cache.clear()
    _pool_tick_cache.clear()
    _wallet_sol_cache.clear()
    _wallet_token_cache.clear()


WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Byte offsets of (mint_x, mint_y) inside each DEX's on-chain pool account,
# plus the owning program id as a guard. All three sets were verified
# empirically on live mainnet pool accounts 2026-10-10 by locating the known
# mints in the raw account bytes:
#   meteora LbPair     (len 904)  tokenXMint @ 88,  tokenYMint @ 120
#   orca    Whirlpool  (len 653)  mintA      @ 101, mintB      @ 181
#   raydium PoolState  (len 1544) tokenMint0 @ 73,  tokenMint1 @ 105
# The owner check makes a wrong-account (or wrong-layout) read fail closed.
_POOL_LAYOUT = {
    "meteora": ((88, 120), "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"),
    "orca": ((101, 181), "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"),
    "raydium": ((73, 105), "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"),
}

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

# Conservative, rails-overridable defaults. The per-DEX overhead covers the
# position account plus (Meteora) bin-array rent the opener funds; it is a
# floor, not an exact rent model, and is intentionally generous.
DEFAULT_PREFLIGHT: Dict[str, Any] = {
    "sol_overhead_lamports": {
        "meteora": 45_000_000,
        "orca": 30_000_000,
        "raydium": 30_000_000,
        "default": 45_000_000,
    },
    "ata_rent_lamports": 2_039_280,
    "base_fee_lamports": 5_000,
    "tx_signatures": 2,
    "sol_reserve_lamports": 5_000_000,
    "priority_fee_lamports": 500_000,
    "rpc_timeout_seconds": 10.0,
}


class PreflightUnavailable(Exception):
    """Live state could not be read; the caller must fail closed."""


# --------------------------------------------------------------------------
# base58 (stdlib only; pubkeys are 32 bytes)
# --------------------------------------------------------------------------

def b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    pad = 0
    for byte in raw:
        if byte != 0:
            break
        pad += 1
    return "1" * pad + out


def b58decode(value: str) -> bytes:
    num = 0
    for char in value:
        num = num * 58 + _B58_INDEX[char]
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = 0
    for char in value:
        if char != "1":
            break
        pad += 1
    return b"\x00" * pad + body


# --------------------------------------------------------------------------
# live RPC reads
# --------------------------------------------------------------------------

def _rpc(rpc_url: str, method: str, params: list, timeout: float) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not isinstance(body, dict) or body.get("error"):
        raise PreflightUnavailable(f"{method}: {body.get('error') if isinstance(body, dict) else body}")
    return body.get("result")


def fetch_sol_lamports(rpc_url: str, wallet: str, timeout: float = 10.0) -> int:
    """Live native SOL balance in lamports."""
    result = _rpc(rpc_url, "getBalance", [wallet], timeout)
    if not isinstance(result, dict) or "value" not in result:
        raise PreflightUnavailable("getBalance returned no value")
    return int(result["value"])


def fetch_token_balances(rpc_url: str, wallet: str, timeout: float = 10.0,
                         mints: Optional[set] = None) -> Dict[str, int]:
    """Live raw SPL token balances keyed by mint.

    Reads both the legacy and Token-2022 programs. A mint present with a zero
    balance still appears (its account exists, so no ATA rent is charged).
    Raises when every program query fails: an empty map must mean "no
    accounts", never "the RPC was unreachable".
    """
    balances: Dict[str, int] = {}
    read_ok = False
    for program in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        try:
            result = _rpc(
                rpc_url, "getTokenAccountsByOwner",
                [wallet, {"programId": program},
                 {"encoding": "jsonParsed", "commitment": "confirmed"}],
                timeout,
            )
        except Exception:
            continue
        read_ok = True
        for account in (result or {}).get("value", []) if isinstance(result, dict) else []:
            parsed = ((account.get("account") or {}).get("data") or {}).get("parsed") or {}
            info = parsed.get("info") or {}
            mint = info.get("mint")
            amount = (info.get("tokenAmount") or {}).get("amount")
            if not mint or amount is None:
                continue
            try:
                raw = int(amount)
            except (TypeError, ValueError):
                continue
            balances[mint] = balances.get(mint, 0) + raw
    if not read_ok:
        raise PreflightUnavailable("getTokenAccountsByOwner failed for both token programs")
    return balances


def fetch_pool_mints(dex: str, pool_address: str, rpc_url: str,
                     timeout: float = 10.0) -> Optional[Tuple[str, str]]:
    """Live (mint_x, mint_y) of a pool, or None when unreadable."""
    layout = _POOL_LAYOUT.get((dex or "").lower())
    if not layout or not pool_address or not rpc_url:
        return None
    (off_x, off_y), program = layout
    result = _rpc(
        rpc_url, "getAccountInfo",
        [pool_address, {"encoding": "base64", "commitment": "confirmed"}],
        timeout,
    )
    value = (result or {}).get("value") if isinstance(result, dict) else None
    if not value:
        return None
    if program and value.get("owner") != program:
        return None
    data = value.get("data")
    raw = data[0] if isinstance(data, list) and data else data
    if not isinstance(raw, str):
        return None
    blob = base64.b64decode(raw)
    if len(blob) < off_y + 32:
        return None
    return (b58encode(blob[off_x:off_x + 32]), b58encode(blob[off_y:off_y + 32]))


# --------------------------------------------------------------------------
# requirement model
# --------------------------------------------------------------------------

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sol_overhead_lamports(rails: Dict[str, Any], dex: str) -> int:
    table = rails.get("sol_overhead_lamports")
    if isinstance(table, dict):
        for key in (dex, "default"):
            if key in table:
                return _as_int(table[key], 0)
    defaults = DEFAULT_PREFLIGHT["sol_overhead_lamports"]
    return _as_int(defaults.get(dex, defaults["default"]))


def _priority_fee_lamports(rails: Dict[str, Any]) -> int:
    explicit = rails.get("priority_fee_lamports")
    if explicit is not None:
        return _as_int(explicit, 0)
    try:
        cap_sol = float(rails.get("priority_fee_cap_sol",
                                  DEFAULT_PREFLIGHT["priority_fee_lamports"] / 1e9))
    except (TypeError, ValueError):
        cap_sol = DEFAULT_PREFLIGHT["priority_fee_lamports"] / 1e9
    return int(cap_sol * 1_000_000_000)


def _declared_mints(signal: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Signal-provided mints, if the producer supplies them."""
    for x_key, y_key in (("mint_x", "mint_y"), ("token_x_mint", "token_y_mint")):
        mint_x, mint_y = signal.get(x_key), signal.get(y_key)
        if isinstance(mint_x, str) and isinstance(mint_y, str) and mint_x and mint_y:
            return mint_x, mint_y
    return None


def _reject(stage: str, error_code: str, **extra: Any) -> Tuple[bool, Dict[str, Any]]:
    return False, {"stage": stage, "error_code": error_code, **extra}


def preflight_open(
    signal: Dict[str, Any],
    cfg: Dict[str, Any],
    rails: Dict[str, Any],
    *,
    sol_fetcher: Optional[Callable[..., int]] = None,
    tokens_fetcher: Optional[Callable[..., Dict[str, int]]] = None,
    mints_fetcher: Optional[Callable[..., Optional[Tuple[str, str]]]] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Live SOL/token preflight for an ``open``.

    Returns ``(True, None)`` when the open may proceed, or
    ``(False, details)`` with a journal-ready rejection entry.
    """
    sol_fetcher = sol_fetcher or fetch_sol_lamports
    tokens_fetcher = tokens_fetcher or fetch_token_balances
    mints_fetcher = mints_fetcher or fetch_pool_mints

    dex = (signal.get("dex") or "meteora").lower()
    pool_address = signal.get("pool_address")
    rpc_url = cfg.get("rpc_https_url")
    wallet_id = signal.get("wallet_id") or "main"
    registry = cfg.get("wallet_registry") or {}
    wallet = registry.get(wallet_id) or cfg.get("wallet_public_key")
    timeout = float(rails.get("rpc_timeout_seconds",
                              DEFAULT_PREFLIGHT["rpc_timeout_seconds"]))

    if not rpc_url or not wallet:
        return _reject("preflight_sol", "PREFLIGHT_UNAVAILABLE",
                       error="missing rpc_https_url or wallet_public_key")

    liquidity = signal.get("liquidity") or {}
    amount_x = _as_int(liquidity.get("amount_x"), 0)
    amount_y = _as_int(liquidity.get("amount_y"), 0)

    mints = _declared_mints(signal)
    if mints is None:
        # Check cache first (keyed by dex + pool_address). A cached value of
        # None means "tried and unreadable" within this wake: fail closed.
        cache_key = (dex, pool_address)
        if cache_key in _pool_mints_cache:
            mints = _pool_mints_cache[cache_key]
            if mints is None:
                return _reject(
                    "preflight_sol", "PREFLIGHT_POOL_UNREADABLE",
                    error=(f"cannot read live pool mints for dex={dex} "
                           f"pool={pool_address}; refusing to open with the "
                           "SOL/token requirement unverified"),
                )
        else:
            try:
                mints = mints_fetcher(dex, pool_address, rpc_url, timeout)
            except Exception as exc:
                _pool_mints_cache[cache_key] = None  # mark as tried
                return _reject("preflight_sol", "PREFLIGHT_POOL_UNREADABLE", error=str(exc))
            if mints is None:
                _pool_mints_cache[cache_key] = None  # mark as tried
                return _reject(
                    "preflight_sol", "PREFLIGHT_POOL_UNREADABLE",
                    error=(f"cannot read live pool mints for dex={dex} "
                           f"pool={pool_address}; refusing to open with the "
                           "SOL/token requirement unverified"),
                )
            _pool_mints_cache[cache_key] = mints  # cache the successful result
    mint_x, mint_y = mints

    # Balances are never cached: a mid-wake swap (or another open's deposit)
    # changes real SOL/USDC state, and preflighting a later signal against a
    # previous leg's balances would double-spend the same lamports on paper
    # and let a real shortfall reach simulation. Live read, every time.
    try:
        native_lamports = int(sol_fetcher(rpc_url, wallet, timeout))
    except Exception as exc:
        return _reject("preflight_sol", "PREFLIGHT_UNAVAILABLE",
                       error=f"live SOL balance unavailable: {exc}")
    try:
        token_balances = tokens_fetcher(rpc_url, wallet, timeout)
    except Exception as exc:
        return _reject("preflight_sol", "PREFLIGHT_UNAVAILABLE",
                       error=f"live token balances unavailable: {exc}")
    token_balances = token_balances or {}

    sol_side = None
    if mint_x == WSOL_MINT:
        sol_side = "x"
    elif mint_y == WSOL_MINT:
        sol_side = "y"
    sol_side_raw = amount_x if sol_side == "x" else (amount_y if sol_side == "y" else 0)
    wsol_balance = token_balances.get(WSOL_MINT, 0) if sol_side else 0

    ata_rent = _as_int(rails.get("ata_rent_lamports", DEFAULT_PREFLIGHT["ata_rent_lamports"]))
    base_fee = _as_int(rails.get("base_fee_lamports", DEFAULT_PREFLIGHT["base_fee_lamports"]))
    tx_signatures = max(1, _as_int(rails.get("tx_signatures", DEFAULT_PREFLIGHT["tx_signatures"])))
    reserve = _as_int(rails.get("sol_reserve_lamports", DEFAULT_PREFLIGHT["sol_reserve_lamports"]))
    overhead = _sol_overhead_lamports(rails, dex)
    priority_fee = _priority_fee_lamports(rails)

    # Rent for any token account the open must create (a mint without a live
    # account). Using jsonParsed balances means an existing zero-balance ATA
    # is known, so we never over-charge rent.
    atas_to_create = [m for m in (mint_x, mint_y) if m not in token_balances]
    ata_rent_total = ata_rent * len(atas_to_create)

    # SOL side: an existing WSOL balance offsets how much native SOL must be
    # wrapped; the remainder is funded from the native balance.
    sol_topup = max(0, sol_side_raw - wsol_balance) if sol_side else 0
    required_lamports = (overhead + reserve + base_fee * tx_signatures
                         + priority_fee + ata_rent_total + sol_topup)

    if required_lamports > native_lamports:
        return _reject(
            "preflight_sol", "INSUFFICIENT_SOL_FOR_OPEN",
            dex=dex,
            wallet_id=wallet_id,
            required_lamports=required_lamports,
            available_lamports=native_lamports,
            shortfall_lamports=required_lamports - native_lamports,
            sol_side=sol_side,
            sol_side_raw=str(sol_side_raw),
            breakdown={
                "overhead_lamports": overhead,
                "reserve_lamports": reserve,
                "fees_lamports": base_fee * tx_signatures + priority_fee,
                "ata_rent_lamports": ata_rent_total,
                "atas_to_create": atas_to_create,
                "sol_topup_lamports": sol_topup,
                "wsol_balance_raw": str(wsol_balance),
            },
        )

    token_x = {"mint": mint_x, "required_raw": str(amount_x), "covered_by_sol": False}
    token_y = {"mint": mint_y, "required_raw": str(amount_y), "covered_by_sol": False}
    failures = []
    for entry, side, mint, required in (
        (token_x, "x", mint_x, amount_x),
        (token_y, "y", mint_y, amount_y),
    ):
        if side == sol_side:
            entry["covered_by_sol"] = True
            entry["available_raw"] = str(wsol_balance)
            continue
        available = token_balances.get(mint, 0)
        entry["available_raw"] = str(available)
        if available < required:
            failures.append((side, required, available))

    if failures:
        details: Dict[str, Any] = {
            "stage": "preflight_tokens",
            "error_code": "INSUFFICIENT_TOKEN_BALANCE",
            "dex": dex,
            "wallet_id": wallet_id,
            "token_x": token_x,
            "token_y": token_y,
        }
        for side, required, available in failures:
            details[f"shortfall_{side}_raw"] = str(required - available)
        return False, details

    return True, None