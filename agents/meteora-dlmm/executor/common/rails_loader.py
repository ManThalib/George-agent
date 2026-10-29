"""Load and parse the safety rails.

The human-readable source is SAFETY_RAILS.md. A JSON mirror
(`execution_limits.json`) is the single source of truth for mechanical
execution rails. Sheldon's policy manifest (`sheldon_policy.json`) is read
for strategy-owned values (bin-step whitelist, position sizing, pool
eligibility) that George still enforces fail-closed.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict

from common import paths
from common.utils import load_json


RAILS_PATH = paths.rails_path()
EXECUTION_LIMITS_PATH = paths.execution_limits_path()
SHELDON_POLICY_PATH = paths.sheldon_policy_path()

# --------------------------------------------------------------------------
# Conservative fallback rails.
#
# INVARIANT (owner decision, 2026-10-29 audit): no fallback here may be LOOSER
# than the value in execution_limits.json. A missing or unreadable limits file
# must narrow what the executor may do, never widen it. The previous table
# violated this in three places — max_daily_loss_usd 50 vs 10 (5x looser),
# min_open_score 70 vs 90 (20 points looser) and max_open_positions 3 vs 9 —
# so a single unreadable file silently disabled part of the loss rail.
#
# Direction matters per key:
#   max_*  / *_cap_*  : smaller is stricter
#   min_*  / floor    : larger  is stricter
#   windows / bools   : see each comment
#
# test_rails_limits.FallbackStrictnessTests pins the invariant, so adding a
# rail without deciding its fallback fails the suite.
# --------------------------------------------------------------------------
DEFAULT_RAILS: Dict[str, Any] = {
    # per-position and portfolio caps (smaller is stricter)
    "max_position_usd": 100.0,
    "min_position_usd": 10.0,
    "max_total_exposure_usd": 300.0,     # live 500: fallback stays narrower
    "max_open_positions": 3,             # live 9: fallback stays narrower
    "max_daily_loss_usd": 10.0,          # live 10: must never be looser
    "max_drawdown_pct": 10.0,
    # circuit breakers
    # NOTE: neither breaker has an enforcement point in this codebase today
    # (no failure counter exists). Listed here because SAFETY_RAILS.md
    # documents them; see the audit report, section 3.8.
    "pause_after_consecutive_failures": 3,
    "pause_on_rpc_error_streak": 5,
    # execution guards
    "max_slippage_bps": 100,
    "min_remove_bps": 1,
    "partial_remove_bps_min": 2500,
    "partial_remove_bps_max": 7500,
    "mutation_cooldown_hours": 6,
    "max_price_impact_pct": 1.5,
    "priority_fee_cap_sol": 0.0005,
    "tx_timeout_seconds": 60,
    "one_tx_at_a_time": True,
    "claim_min_usd": 1.0,                # live 1.0; 0 would claim dust for gas
    # pool eligibility (larger floors are stricter)
    "min_pool_liquidity_usd": 250000.0,
    "min_24h_volume_usd": 1000000.0,
    # scoring floor (larger is stricter)
    "min_open_score": 90.0,              # live 90: must never be looser
    "min_open_score_adaptive_floor": 55.0,
    # range limits
    "bin_step_rule": {"min": 4, "max": 400},
    "max_bin_range_width": 2000,
    "max_meteora_range_width": 70,
    "max_orca_range_width": 2000,
    "max_raydium_range_width": 2000,
    "reject_active_bin_out_of_range_open": True,
    # Documented in SAFETY_RAILS.md, but NOT enforced by any executor code
    # path today (they are producer-side scheduling hints). Kept so the
    # markdown mirror keeps parsing; do not treat them as live rails.
    "open_window_utc": "00:00-23:59",
    "close_window_utc": "00:00-23:59",
    "blackout_dates": [],
}


def _load_execution_limits() -> Dict[str, Any]:
    """Load mechanical execution limits from JSON.

    Flatten nested groups so existing callers can keep using simple keys.
    """
    limits: Dict[str, Any] = {}
    try:
        data = load_json(EXECUTION_LIMITS_PATH)
    except Exception:
        return limits

    if not isinstance(data, dict):
        return limits

    for group in (
        "position_sizing",
        "exposure_and_loss",
        "circuit_breakers",
        "execution_guards",
        "range_limits",
        "preflight",
    ):
        for key, val in (data.get(group) or {}).items():
            limits[key] = val
    return limits


def _load_sheldon_policy() -> Dict[str, Any]:
    """Load strategy-owned policy values that George enforces fail-closed."""
    policy: Dict[str, Any] = {}
    try:
        data = load_json(SHELDON_POLICY_PATH)
    except Exception:
        return policy

    if not isinstance(data, dict):
        return policy

    scoring = data.get("scoring") or {}
    pool = data.get("pool_eligibility") or {}
    policy["min_pool_liquidity_usd"] = pool.get("min_pool_liquidity_usd")
    policy["min_24h_volume_usd"] = pool.get("min_24h_volume_usd")
    # min_open_score moved to the `scoring` block (Sheldon Phase 2, 2026-10-04).
    # Read the new path first; fall back to the legacy path for older manifests.
    policy["min_open_score"] = scoring.get("min_open_score",
                                           pool.get("min_open_score"))
    policy["scoring_source"] = scoring.get("source")
    # bin_step is NOT overridden from Sheldon's policy: execution_limits.json
    # is the single source of truth for George's rails (owner decision
    # 2026-10-19). Sheldon's copy previously overrode this file here, which
    # is how the two drifted apart.

    sizing = data.get("position_sizing") or {}
    policy["min_position_usd"] = sizing.get("min_position_usd")
    policy["max_position_usd"] = sizing.get("default_max_position_usd")

    windows = data.get("windows") or {}
    policy["open_window_utc"] = windows.get("open_window_utc")
    policy["close_window_utc"] = windows.get("close_window_utc")
    policy["blackout_dates"] = windows.get("blackout_dates")

    return policy


def _parse_int_list(raw: str) -> list:
    """Parse a markdown rail value like '[10, 20, 25, 50, 100]' into ints."""
    cleaned = raw.strip().strip("[]").replace(" ", "")
    if not cleaned:
        raise ValueError("empty list")
    return [int(part) for part in cleaned.split(",") if part]


def _parse_bin_step_rule(raw: str) -> dict:
    """Parse a rail value like '[4..400]' or '4-400' into a rule dict."""
    cleaned = raw.strip().strip("[]").replace(" ", "")
    for sep in ("..", "-"):
        if sep in cleaned:
            lo, hi = cleaned.split(sep, 1)
            return {"min": int(lo), "max": int(hi)}
    raise ValueError("expected <min>..<max>")


_RAIL_PARSERS = {
    "max_position_usd": float,
    "min_position_usd": float,
    "max_total_exposure_usd": float,
    "max_open_positions": int,
    "max_daily_loss_usd": float,
    "max_drawdown_pct": float,
    "pause_after_consecutive_failures": int,
    "pause_on_rpc_error_streak": int,
    "max_slippage_bps": int,
    "max_price_impact_pct": float,
    "priority_fee_cap_sol": float,
    "tx_timeout_seconds": int,
    "bin_step_rule": _parse_bin_step_rule,
    "max_bin_range_width": int,
    "max_meteora_range_width": int,
    "max_orca_range_width": int,
    "max_raydium_range_width": int,
    "min_pool_liquidity_usd": float,
    "min_24h_volume_usd": float,
    "min_open_score": float,
}


def _parse_markdown_rails(path: Path) -> Dict[str, Any]:
    """Best-effort parse of SAFETY_RAILS.md numeric tables."""
    rails = dict(DEFAULT_RAILS)
    if not path.exists():
        return rails
    text = path.read_text(encoding="utf-8")
    for key, caster in _RAIL_PARSERS.items():
        # Rail keys are written as `key` in the markdown tables; the leading
        # and trailing backticks are optional so plain keys also match.
        pattern = rf"`?{re.escape(key)}`?\s*\|\s*(?:\*\*)?([^|\n]*?)(?:\*\*)?(?=\s*\|)"
        match = re.search(pattern, text)
        if match:
            raw = match.group(1).strip()
            if raw == "true":
                rails[key] = True
            elif raw == "false":
                rails[key] = False
            else:
                try:
                    rails[key] = caster(raw.rstrip("*"))
                except (TypeError, ValueError):
                    pass
    return rails


def load_rails() -> Dict[str, Any]:
    """Load rails from the JSON single-sources, with a markdown doc fallback.

    Priority (later wins):
      1. conservative built-in defaults (never looser than execution_limits)
      2. execution_limits.json (mechanical limits — the source of truth)
      3. sheldon_policy.json (strategy-owned values George enforces)
      4. SAFETY_RAILS.md, for keys no JSON source provides

    The former step "legacy SAFETY_RAILS.json mirror" was removed: it merged
    ``{**DEFAULT_RAILS, **mirror}`` back over the already-resolved rails, so
    any key the mirror omitted was silently REVERTED to the built-in default
    (i.e. adding the file could loosen max_daily_loss from 10 to 50). The file
    does not exist in any deployment, so the branch was dead as well as
    dangerous.

    The markdown fallback only fills keys that neither the defaults nor the
    JSON sources set, so a documentation edit can never widen a mechanical
    limit.
    """
    rails = dict(DEFAULT_RAILS)

    # Mechanical execution limits are the primary source.
    rails.update(_load_execution_limits())

    # Strategy-owned policy values that George still enforces fail-closed.
    policy = _load_sheldon_policy()
    rails.update({k: v for k, v in policy.items() if v is not None})

    for key, val in _parse_markdown_rails(RAILS_PATH).items():
        if key not in rails:
            rails[key] = val
    return rails
