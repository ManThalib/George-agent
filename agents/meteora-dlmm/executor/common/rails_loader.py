"""Load and parse the safety rails.

The source of truth is SAFETY_RAILS.md. A mirror `rails.json` file is kept
in sync for the executor's convenience. If the JSON is missing, the executor
falls back to conservative defaults and warns.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict

from common.utils import load_json


RAILS_PATH = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/SAFETY_RAILS.md")
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
    "max_price_impact_pct": 1.5,
    "priority_fee_cap_sol": 0.0005,
    "tx_timeout_seconds": 60,
    "one_tx_at_a_time": True,
    "min_pool_liquidity_usd": 250000.0,
    "min_24h_volume_usd": 1000000.0,
    "allowed_bin_steps": [10, 20, 25, 50, 100],
    "max_bin_range_width": 2000,
    "max_meteora_range_width": 70,
    "reject_active_bin_out_of_range_open": True,
    "open_window_utc": "00:00-23:59",
    "close_window_utc": "00:00-23:59",
    "blackout_dates": [],
}


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
    "max_bin_range_width": int,
    "max_meteora_range_width": int,
    "min_pool_liquidity_usd": float,
    "min_24h_volume_usd": float,
}


def _parse_markdown_rails(path: Path) -> Dict[str, Any]:
    """Best-effort parse of SAFETY_RAILS.md numeric tables."""
    rails = dict(DEFAULT_RAILS)
    if not path.exists():
        return rails
    text = path.read_text(encoding="utf-8")
    for key, caster in _RAIL_PARSERS.items():
        pattern = rf"{re.escape(key)}\s*\|\s*(?:\*\*)?([^|\n]*?)(?:\*\*)?(?=\s*\|)"
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
    """Load rails. Prefer JSON mirror, else parse markdown, else defaults."""
    json_path = RAILS_PATH.with_suffix(".json")
    if json_path.exists():
        try:
            return {**DEFAULT_RAILS, **load_json(json_path)}
        except Exception:
            pass
    return _parse_markdown_rails(RAILS_PATH)
