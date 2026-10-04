"""Stateful exposure, daily-loss, and drawdown guard.

The executor re-reads this state before every OPEN transaction. The state is
kept in `state/exposure_state.json` and updated as the dispatcher executes
opens and closes.

Sheldon provides a `position_usd` field on OPEN signals. The guard uses that
value together with the stored state to enforce the rails documented in
SAFETY_RAILS.md and execution_limits.json.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STATE_DIR = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/state")
STATE_PATH = STATE_DIR / "exposure_state.json"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state() -> Dict[str, Any]:
    """Return the current guard state, defaulting to a fresh empty state."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {
        "open_positions": [],
        "daily_loss_usd": 0.0,
        "max_daily_loss_usd": 0.0,
        "peak_portfolio_usd": 0.0,
        "session_start_value_usd": 0.0,
        "last_reset_date": _today(),
    }


def save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def reset_daily_if_needed(state: Dict[str, Any]) -> Dict[str, Any]:
    """Zero daily counters when the UTC date rolls over."""
    if state.get("last_reset_date") != _today():
        state["daily_loss_usd"] = 0.0
        state["last_reset_date"] = _today()
    return state


def _mutation_cooldown_hours(rails: Dict[str, Any]) -> float:
    return float(rails.get("mutation_cooldown_hours", 6.0))


def _last_mutation_at(state: Dict[str, Any], position_id: str) -> Optional[datetime]:
    last = (state.get("last_mutations") or {}).get(position_id)
    if not isinstance(last, dict):
        return None
    try:
        return datetime.fromisoformat(last.get("at") or "")
    except (ValueError, TypeError):
        return None


def _record_mutation(state: Dict[str, Any], signal: Dict[str, Any]) -> None:
    position_id = signal.get("position_id")
    if not position_id:
        return
    if "last_mutations" not in state:
        state["last_mutations"] = {}
    state["last_mutations"][position_id] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "action": signal.get("action"),
    }


def _total_exposure(open_positions: List[Dict[str, Any]]) -> float:
    return sum(float(p.get("position_usd", 0.0)) for p in open_positions)


def _drawdown_pct(state: Dict[str, Any]) -> float:
    peak = float(state.get("peak_portfolio_usd") or 0.0)
    current = float(state.get("session_start_value_usd") or 0.0)
    if peak <= 0 or current <= 0:
        return 0.0
    return (peak - current) / peak * 100.0


def check_open_allowed(signal: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (allowed, reason_or_empty).

    Checks (scoped to the signal's wallet — mirror wallets have their own
    position/exposure budgets; MAIN's usage must not consume a mirror's):
      - per-wallet open_positions < max_open_positions
      - per-wallet total_exposure + position_usd <= max_total_exposure_usd
      - position_usd inside [min_position_usd, max_position_usd]
      - daily_loss_usd < max_daily_loss_usd (global: losses are losses)
      - drawdown_pct < max_drawdown_pct (global)
    """
    state = reset_daily_if_needed(load_state())

    position_usd = signal.get("position_usd")
    try:
        position_usd = float(position_usd)
    except (TypeError, ValueError):
        return False, "position_usd missing or non-numeric; fail-closed"

    min_pos = float(rails.get("min_position_usd", 10.0))
    max_pos = float(rails.get("max_position_usd", 100.0))
    if position_usd < min_pos:
        return False, f"position_usd {position_usd:.2f} < min {min_pos:.2f}"
    if position_usd > max_pos:
        return False, f"position_usd {position_usd:.2f} > max {max_pos:.2f}"

    wallet_id = signal.get("wallet_id") or "main"
    all_positions = state.get("open_positions") or []
    open_positions = [
        p for p in all_positions if (p.get("wallet_id") or "main") == wallet_id
    ]
    max_open = int(rails.get("max_open_positions", 3))
    if len(open_positions) >= max_open:
        return False, f"open positions {len(open_positions)} >= max {max_open} (wallet {wallet_id})"

    max_exposure = float(rails.get("max_total_exposure_usd", 300.0))
    current_exposure = _total_exposure(open_positions)
    if current_exposure + position_usd > max_exposure:
        return False, (
            f"exposure ${current_exposure + position_usd:.2f} > max "
            f"${max_exposure:.2f} (wallet {wallet_id})"
        )

    max_daily_loss = float(rails.get("max_daily_loss_usd", 50.0))
    daily_loss = float(state.get("daily_loss_usd") or 0.0)
    if daily_loss >= max_daily_loss:
        return False, f"daily loss ${daily_loss:.2f} >= max ${max_daily_loss:.2f}"

    max_drawdown = float(rails.get("max_drawdown_pct", 10.0))
    dd = _drawdown_pct(state)
    if dd >= max_drawdown:
        return False, f"drawdown {dd:.2f}% >= max {max_drawdown:.2f}%"

    return True, ""


def record_open_executed(signal: Dict[str, Any]) -> None:
    """Call after an OPEN transaction has been confirmed."""
    state = load_state()
    open_positions: List[Dict[str, Any]] = state.get("open_positions") or []
    open_positions.append({
        "pool_address": signal.get("pool_address"),
        "position_id": signal.get("position_id"),
        "wallet_id": signal.get("wallet_id") or "main",
        "position_usd": float(signal.get("position_usd") or 0.0),
        "opened_at": datetime.now(timezone.utc).isoformat(),
    })
    state["open_positions"] = open_positions
    save_state(state)


def _find_position(open_positions: List[Dict[str, Any]], signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Match a stored position entry by position_id, falling back to pool_address.

    Entries opened before position_id tracking have no position_id; those
    can only be addressed by pool (ambiguous only if the pool holds several).
    """
    position_id = signal.get("position_id")
    pool_address = signal.get("pool_address")
    if position_id:
        for p in open_positions:
            if p.get("position_id") == position_id:
                return p
    for p in open_positions:
        if p.get("pool_address") == pool_address and not p.get("position_id"):
            return p
    return None


def check_add_allowed(signal: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (allowed, reason_or_empty) for an add_liquidity signal.

    An add deploys new capital, so the loss/drawdown/exposure rails apply
    like an open. max_open_positions is skipped (no new position), and the
    per-position cap is checked against tracked value + add when the
    position is tracked; untracked (pre-tracking) positions fall back to
    checking the add alone against max_position_usd.
    """
    state = reset_daily_if_needed(load_state())

    try:
        add_usd = float(signal.get("position_usd"))
    except (TypeError, ValueError):
        return False, "position_usd missing or non-numeric; fail-closed"
    if add_usd <= 0:
        return False, f"add position_usd must be positive: {add_usd}"

    max_pos = float(rails.get("max_position_usd", 100.0))
    open_positions = state.get("open_positions") or []
    tracked = _find_position(open_positions, signal)
    base_usd = float(tracked.get("position_usd") or 0.0) if tracked else 0.0
    if base_usd + add_usd > max_pos:
        return False, (
            f"position value after add ${base_usd + add_usd:.2f} > max_position_usd {max_pos:.2f}"
        )

    max_exposure = float(rails.get("max_total_exposure_usd", 300.0))
    current_exposure = _total_exposure(open_positions)
    if current_exposure + add_usd > max_exposure:
        return False, (
            f"exposure ${current_exposure + add_usd:.2f} > max "
            f"${max_exposure:.2f}"
        )

    max_daily_loss = float(rails.get("max_daily_loss_usd", 50.0))
    daily_loss = float(state.get("daily_loss_usd") or 0.0)
    if daily_loss >= max_daily_loss:
        return False, f"daily loss ${daily_loss:.2f} >= max ${max_daily_loss:.2f}"

    max_drawdown = float(rails.get("max_drawdown_pct", 10.0))
    dd = _drawdown_pct(state)
    if dd >= max_drawdown:
        return False, f"drawdown {dd:.2f}% >= max {max_drawdown:.2f}%"

    allowed, reason = _check_mutation_cooldown(signal, rails, state)
    if not allowed:
        return False, reason

    return True, ""


def _check_mutation_cooldown(signal: Dict[str, Any], rails: Dict[str, Any], state: Dict[str, Any]) -> Tuple[bool, str]:
    position_id = signal.get("position_id")
    if not position_id:
        return True, ""
    last_at = _last_mutation_at(state, position_id)
    if last_at is None:
        return True, ""
    cooldown = _mutation_cooldown_hours(rails)
    if (datetime.now(timezone.utc) - last_at).total_seconds() < cooldown * 3600:
        return False, (
            f"mutation cooldown active for {position_id}: last mutation at "
            f"{last_at.isoformat()} < {cooldown}h ago"
        )
    return True, ""


def record_add_executed(signal: Dict[str, Any]) -> None:
    """Call after an add_liquidity transaction has been confirmed.

    Grows the tracked position_usd (creating an entry when the position is
    not yet tracked, e.g. opened before position_id tracking existed).
    """
    state = load_state()
    open_positions: List[Dict[str, Any]] = state.get("open_positions") or []
    add_usd = float(signal.get("position_usd") or 0.0)
    tracked = _find_position(open_positions, signal)
    if tracked is not None:
        tracked["position_usd"] = float(tracked.get("position_usd") or 0.0) + add_usd
        if signal.get("position_id") and not tracked.get("position_id"):
            tracked["position_id"] = signal.get("position_id")
    else:
        open_positions.append({
            "pool_address": signal.get("pool_address"),
            "position_id": signal.get("position_id"),
            "position_usd": add_usd,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "note": "created by add_liquidity on untracked position",
        })
    _record_mutation(state, signal)
    state["open_positions"] = open_positions
    save_state(state)


def record_remove_executed(signal: Dict[str, Any]) -> None:
    """Call after a remove_liquidity transaction has been confirmed.

    Shrinks the tracked position_usd proportionally to the removed bps. The
    entry stays: the position account remains open on-chain until a close.
    """
    state = load_state()
    open_positions: List[Dict[str, Any]] = state.get("open_positions") or []
    bps = int(signal.get("bps") or 0)
    tracked = _find_position(open_positions, signal)
    if tracked is not None:
        tracked["position_usd"] = float(tracked.get("position_usd") or 0.0) * (1.0 - bps / 10000.0)
    _record_mutation(state, signal)
    state["open_positions"] = open_positions
    save_state(state)


def check_remove_allowed(signal: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (allowed, reason_or_empty) for a remove_liquidity signal.

    Removes are de-risking, so loss/drawdown/exposure rails do not apply.
    We do enforce a minimum remaining position value (you cannot strip a
    position below the minimum economic size) and the mutation cooldown.
    """
    state = reset_daily_if_needed(load_state())

    try:
        bps = int(signal.get("bps") or 0)
    except (TypeError, ValueError):
        return False, "remove_liquidity bps missing or non-integer"

    position_usd = signal.get("position_usd")
    if position_usd is not None:
        try:
            position_usd = float(position_usd)
        except (TypeError, ValueError):
            position_usd = None
    if position_usd is not None:
        remaining = position_usd * (1.0 - bps / 10000.0)
        min_pos = float(rails.get("min_position_usd", 10.0))
        if remaining < min_pos:
            return False, (
                f"remaining position value ${remaining:.2f} < min_position_usd {min_pos:.2f}"
            )

    allowed, reason = _check_mutation_cooldown(signal, rails, state)
    if not allowed:
        return False, reason

    return True, ""


def check_rebalance_allowed(signal: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (allowed, reason_or_empty) for a rebalance signal.

    A rebalance closes an existing range and reopens in a new range. Net
    exposure change is new_position_usd - old_position_usd. The new size
    must still fit within min/max_position_usd.
    """
    return _check_replace_position(signal, rails, "rebalance")


def check_rotate_allowed(signal: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (allowed, reason_or_empty) for a rotate signal.

    A rotate closes one position and opens another in a different pool.
    Exposure logic is the same as rebalance.
    """
    return _check_replace_position(signal, rails, "rotate")


def _check_replace_position(signal: Dict[str, Any], rails: Dict[str, Any], action: str) -> Tuple[bool, str]:
    state = reset_daily_if_needed(load_state())

    try:
        position_usd = float(signal.get("position_usd"))
    except (TypeError, ValueError):
        return False, f"{action} signal missing position_usd; cannot verify sizing rails"

    min_pos = float(rails.get("min_position_usd", 10.0))
    max_pos = float(rails.get("max_position_usd", 100.0))
    if position_usd < min_pos:
        return False, f"position_usd {position_usd:.2f} < min_position_usd {min_pos:.2f}"
    if position_usd > max_pos:
        return False, f"position_usd {position_usd:.2f} > max_position_usd {max_pos:.2f}"

    max_exposure = float(rails.get("max_total_exposure_usd", 300.0))
    open_positions = state.get("open_positions") or []
    current_exposure = _total_exposure(open_positions)
    tracked = _find_position(open_positions, signal)
    old_value = float(tracked.get("position_usd") or 0.0) if tracked else 0.0
    net_exposure = current_exposure - old_value + position_usd
    if net_exposure > max_exposure:
        return False, (
            f"net exposure after {action} ${net_exposure:.2f} > max ${max_exposure:.2f}"
        )

    max_daily_loss = float(rails.get("max_daily_loss_usd", 50.0))
    daily_loss = float(state.get("daily_loss_usd") or 0.0)
    if daily_loss >= max_daily_loss:
        return False, f"daily loss ${daily_loss:.2f} >= max ${max_daily_loss:.2f}"

    max_drawdown = float(rails.get("max_drawdown_pct", 10.0))
    dd = _drawdown_pct(state)
    if dd >= max_drawdown:
        return False, f"drawdown {dd:.2f}% >= max {max_drawdown:.2f}%"

    allowed, reason = _check_mutation_cooldown(signal, rails, state)
    if not allowed:
        return False, reason

    return True, ""


def record_close_executed(signal: Dict[str, Any], realized_pnl_usd: float = 0.0) -> None:
    """Call after a CLOSE transaction has been confirmed.

    Removes the matching open position and books any realized loss toward
    the daily loss counter.
    """
    state = load_state()
    open_positions: List[Dict[str, Any]] = state.get("open_positions") or []
    pool_address = signal.get("pool_address")
    wallet_id = signal.get("wallet_id") or "main"
    # Key on (wallet_id, pool): the same pool can be open on MAIN and a
    # mirror; pool-only keys would collide and close both legs.
    open_positions = [
        p for p in open_positions
        if not (p.get("pool_address") == pool_address
                and (p.get("wallet_id") or "main") == wallet_id)
    ]
    state["open_positions"] = open_positions
    if realized_pnl_usd < 0:
        state["daily_loss_usd"] = float(state.get("daily_loss_usd") or 0.0) - realized_pnl_usd
    save_state(state)
