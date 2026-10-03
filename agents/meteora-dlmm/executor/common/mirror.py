"""Multi-wallet mirror: expansion of MAIN decisions into per-wallet legs.

Design (PLAN.md workstream C, owner-approved 2026-10-03): Sheldon decides
once on MAIN. Every registered mirror wallet copies that decision with its
own capital, its own keypair, its own position NFT. Mirrors have NO brain —
no scoring, no sizing, no independent policy.

Leg semantics:
- The MAIN leg runs first (it already is the original signal).
- A mirror OPEN runs only if the MAIN leg succeeded.
- A mirror CLOSE/CLAIM runs even if MAIN's leg failed (de-risking both
  wallets must not depend on one leg), but drift is flagged.
- Any drift between MAIN and a mirror is recorded in
  state/mirror_sync.json and surfaced by check_drift(); a mirror OPEN
  failure waits for the next cycle + alerts the owner — never silently
  retried within the same run.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..",
    "state",
    "mirror_sync.json",
)

MIRRORABLE_ACTIONS = {"open", "close", "claim_fees", "claim_rewards", "add_liquidity", "remove_liquidity"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sync_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {"legs": {}, "drift": []}


def save_sync_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def record_leg(signal_id: str, wallet_id: str, action: str, status: str, detail: str = "") -> None:
    """Record one leg's outcome, keyed (signal_id, wallet_id)."""
    state = load_sync_state()
    legs = state.setdefault("legs", {})
    legs[f"{signal_id}:{wallet_id}"] = {
        "signal_id": signal_id,
        "wallet_id": wallet_id,
        "action": action,
        "status": status,
        "detail": detail,
        "at": _utc_now(),
    }
    save_sync_state(state)


def record_drift(signal_id: str, wallet_id: str, action: str, reason: str) -> None:
    """Flag MAIN/mirror divergence. Surfaced by check_drift; never auto-retried."""
    state = load_sync_state()
    drift = state.setdefault("drift", [])
    drift.append({
        "signal_id": signal_id,
        "wallet_id": wallet_id,
        "action": action,
        "reason": reason,
        "at": _utc_now(),
    })
    state["drift"] = drift[-200:]
    save_sync_state(state)


def check_drift() -> List[Dict[str, Any]]:
    """Return unresolved drift entries (for the run epilogue alert)."""
    state = load_sync_state()
    return list(state.get("drift") or [])


def clear_drift(signal_id: Optional[str] = None, wallet_id: Optional[str] = None) -> int:
    """Clear drift entries (after the owner acknowledges or a retry lands)."""
    state = load_sync_state()
    drift = state.get("drift") or []
    kept = [
        d for d in drift
        if (signal_id and d.get("signal_id") != signal_id)
        or (wallet_id and d.get("wallet_id") != wallet_id)
    ]
    removed = len(drift) - len(kept)
    state["drift"] = kept
    save_sync_state(state)
    return removed


def mirror_wallet_ids(registry: Dict[str, str]) -> List[str]:
    """Mirror ids from a wallet_id -> pubkey registry (MAIN excluded)."""
    return sorted(w for w in registry if w != "main")


def build_mirror_signal(signal: Dict[str, Any], wallet_id: str, capital_fraction: float = 1.0) -> Optional[Dict[str, Any]]:
    """Clone a MAIN signal into a mirror leg with scaled capital.

    Sizing: the mirror copies MAIN's structure (pool, range, action) with
    amounts scaled by capital_fraction (of MAIN's position value). There is
    deliberately no re-scoring and no independent policy: mirrors follow.

    Only mirrorable actions produce legs; swap/swap_to_usdc are MAIN-only
    (a mirror's dust sweep is its own capital event, out of scope here).
    """
    action = signal.get("action")
    if action not in MIRRORABLE_ACTIONS:
        return None
    if not (0 < capital_fraction <= 1.0):
        return None
    leg = json.loads(json.dumps(signal))  # deep copy
    leg["signal_id"] = f"{signal.get('signal_id')}:{wallet_id}"
    leg["wallet_id"] = wallet_id
    leg["mirror_of"] = signal.get("signal_id")

    if action in {"open", "add_liquidity"}:
        liquidity = leg.get("liquidity") or {}
        for key in ("amount_x", "amount_y"):
            raw = liquidity.get(key)
            if raw:
                leg["liquidity"][key] = str(int(float(raw) * capital_fraction))
        usd = leg.get("position_usd")
        if usd is not None:
            leg["position_usd"] = round(float(usd) * capital_fraction, 2)
    if action == "remove_liquidity":
        pass  # bps copies unchanged: the mirror removes the same fraction of ITS position

    reason = leg.get("reason") or ""
    leg["reason"] = f"[mirror:{wallet_id} x{capital_fraction:g}] {reason}"
    return leg
