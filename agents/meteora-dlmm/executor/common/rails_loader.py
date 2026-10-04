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

from common.utils import load_json


RAILS_PATH = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/SAFETY_RAILS.md")
EXECUTION_LIMITS_PATH = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/execution_limits.json")
SHELDON_POLICY_PATH = Path("/data/.openclaw/workspace-agents/sheldon/scoring/sheldon_policy.json")

DEFAULT_RAILS: Dict[str, Any] = {
    "max_position_usd": 100.0,
    "min_position_usd": 10.0,
    "max_total_exposure_usd": 300.0,
    "max_open_positions": 3,
    "max_daily_loss_usd": 50.0,
    "max_drawdown_pct": 10.0,
    "pause_after_consecutive_failures": 3,
    "pause_on_rpc_error_streak": 5,
    "max_slippage_bps": 100,
    "min_remove_bps": 1,
    "partial_remove_bps_min": 2500,
    "partial_remove_bps_max": 7500,
    "mutation_cooldown_hours": 6,
    "max_price_impact_pct": 1.5,
    "priority_fee_cap_sol": 0.0005,
    "tx_timeout_seconds": 60,
    "one_tx_at_a_time": True,
    "min_pool_liquidity_usd": 250000.0,
    "min_24h_volume_usd": 1000000.0,
    "min_open_score": 70.0,
    "min_open_score_adaptive_floor": 55.0,
    "allowed_bin_steps": [10, 20, 25, 50, 100],
    "max_bin_range_width": 2000,
    "max_meteora_range_width": 70,
    "max_orca_range_width": 2000,
    "max_raydium_range_width": 2000,
    "reject_active_bin_out_of_range_open": True,
    "open_window_utc": "00:00-23:59",
    "close_window_utc": "00:00-23:59",
    "blackout_dates": [],
}


def _nested_get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key, default)
    return d


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
    policy["allowed_bin_steps"] = pool.get("allowed_bin_steps")

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
    "allowed_bin_steps": _parse_int_list,
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
    """Load rails from JSON single-sources, with markdown fallback.

    Priority:
      1. execution_limits.json (mechanical limits)
      2. sheldon_policy.json (strategy-owned values George enforces)
      3. SAFETY_RAILS.md / rails.json (documentation / legacy mirror)
      4. built-in conservative defaults
    """
    rails = dict(DEFAULT_RAILS)

    # Mechanical execution limits are the primary source.
    limits = _load_execution_limits()
    rails.update(limits)

    # Strategy-owned policy values that George still enforces fail-closed.
    policy = _load_sheldon_policy()
    rails.update({k: v for k, v in policy.items() if v is not None})

    # Legacy JSON mirror (kept for backwards compatibility).
    json_path = RAILS_PATH.with_suffix(".json")
    if json_path.exists():
        try:
            rails.update({**DEFAULT_RAILS, **load_json(json_path)})
        except Exception:
            pass
    else:
        # No JSON mirror -> parse the markdown for any missing keys.
        markdown_rails = _parse_markdown_rails(RAILS_PATH)
        for key, val in markdown_rails.items():
            if key not in rails:
                rails[key] = val

    return rails
