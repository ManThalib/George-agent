"""Consecutive-failure circuit breaker (SAFETY_RAILS.md circuit_breakers).

AGENTS.md and SAFETY_RAILS.md both promise the same behaviour:

    "Failures: count them. 3 consecutive → self-pause, alert (critical)."
    `pause_after_consecutive_failures` | **3** | Failed txs in a row → self-pause
    `pause_on_rpc_error_streak`        | **5** | Consecutive RPC errors → pause

Neither rail had an implementation: the keys were loaded and never read, so a
chain that failed every transaction kept re-signing until a human noticed. This
module is that counter.

Semantics (deliberate, and narrow):

  * only an *execution failure* counts — a rail rejection, a queued dust swap or
    a dry-run is a decision, not a failure, and is neutral;
  * a run of failures is broken only by a successful execution;
  * both rails are checked: consecutive failures and consecutive RPC errors.
    RPC errors are a subset of failures, so with the shipped values (3 failures
    / 5 RPC) the failure rail trips first; the RPC rail adds resolution when it
    is configured BELOW the failure cap (e.g. stop after two unreachable-RPC
    attempts);
  * the pause is sticky: nothing resumes it automatically, because the whole
    point is that repeated failures need a human look. ``resume()`` (wired to
    ``dispatcher.py --resume``) is the deliberate exit;
  * the state is persisted, so a restart does not forget three failures.

The breaker never decides *whether* a transaction is allowed — the rails do
that. It stops the executor from trying at all until an owner says otherwise.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from common import paths

STATE_PATH = paths.state_dir() / "circuit_breaker.json"

DEFAULT_PAUSE_AFTER_FAILURES = 3
DEFAULT_PAUSE_ON_RPC_STREAK = 5

# Substrings that mark a failure as transport/RPC rather than a program error.
# Deliberately broad: a false positive only pauses earlier, which is the safe
# direction; a false negative keeps trying, which is not.
_RPC_MARKERS = (
    "rpc", "429", "502", "503", "504", "timeout", "timed out", "etimedout",
    "econnreset", "econnrefused", "fetch failed", "connection", "socket hang up",
    "blockhash", "node is behind", "rate limit",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty() -> Dict[str, Any]:
    return {
        "consecutive_failures": 0,
        "consecutive_rpc_errors": 0,
        "paused": False,
        "paused_at": None,
        "paused_reason": None,
        "last_failure_at": None,
        "last_success_at": None,
        "total_failures": 0,
    }


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    """Breaker state, defaulting to a clean (not paused) record."""
    target = Path(path) if path else STATE_PATH
    try:
        with open(target, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            merged = _empty()
            merged.update(state)
            return merged
    except Exception:
        pass
    return _empty()


def save(state: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Atomic write: a half-written breaker file must not read as 'not paused'."""
    target = Path(path) if path else STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, target)


def is_paused(path: Optional[Path] = None) -> Tuple[bool, str]:
    """(paused, reason). Unreadable state is *not* treated as a pause."""
    state = load(path)
    if state.get("paused"):
        return True, str(state.get("paused_reason") or "paused by circuit breaker")
    return False, ""


def looks_like_rpc_error(text: Any) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _RPC_MARKERS)


def _threshold(rails: Dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(rails.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def record_success(path: Optional[Path] = None) -> Dict[str, Any]:
    """A transaction landed: both streaks are broken."""
    state = load(path)
    state["consecutive_failures"] = 0
    state["consecutive_rpc_errors"] = 0
    state["last_success_at"] = _now()
    save(state, path)
    return state


def record_failure(rpc: bool, rails: Dict[str, Any],
                   path: Optional[Path] = None) -> Dict[str, Any]:
    """Count one execution failure; trip the breaker when a rail is reached.

    Returns the new state plus ``tripped`` and ``reason`` for the caller's
    critical alert.
    """
    state = load(path)
    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    state["total_failures"] = int(state.get("total_failures") or 0) + 1
    state["last_failure_at"] = _now()
    if rpc:
        state["consecutive_rpc_errors"] = int(state.get("consecutive_rpc_errors") or 0) + 1
    else:
        # A program/slippage failure breaks the RPC streak but not the failure
        # streak: the chain is reachable, it just refused.
        state["consecutive_rpc_errors"] = 0

    fail_cap = _threshold(rails, "pause_after_consecutive_failures",
                          DEFAULT_PAUSE_AFTER_FAILURES)
    rpc_cap = _threshold(rails, "pause_on_rpc_error_streak",
                         DEFAULT_PAUSE_ON_RPC_STREAK)

    reason = ""
    if state["consecutive_failures"] >= fail_cap:
        reason = (f"{state['consecutive_failures']} consecutive execution "
                  f"failures (rail pause_after_consecutive_failures={fail_cap})")
    elif state["consecutive_rpc_errors"] >= rpc_cap:
        reason = (f"{state['consecutive_rpc_errors']} consecutive RPC errors "
                  f"(rail pause_on_rpc_error_streak={rpc_cap})")

    if reason and not state.get("paused"):
        state["paused"] = True
        state["paused_at"] = _now()
        state["paused_reason"] = reason
    save(state, path)
    return {**state, "tripped": bool(reason and state.get("paused")),
            "reason": reason}


def resume(note: str = "", path: Optional[Path] = None) -> Dict[str, Any]:
    """Owner action: clear the pause and both streaks."""
    state = load(path)
    state["paused"] = False
    state["paused_at"] = None
    state["paused_reason"] = None
    state["consecutive_failures"] = 0
    state["consecutive_rpc_errors"] = 0
    state["resumed_at"] = _now()
    state["resumed_note"] = note
    save(state, path)
    return state
