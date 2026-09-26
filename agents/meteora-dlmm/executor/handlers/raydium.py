"""Dry-run handler for Raydium CLMM positions."""

from typing import Any, Dict

from .common import dry_run_result


def open_position(signal: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    notes = (
        f"Raydium CLMM open: pool={signal.get('pool_address')} "
        f"ticks={signal.get('bin_range')} liquidity={signal.get('liquidity')}"
    )
    return dry_run_result("open", signal, cfg, notes=notes)


def close_position(signal: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    notes = f"Raydium CLMM close: position={signal.get('position_id')} pool={signal.get('pool_address')}"
    return dry_run_result("close", signal, cfg, notes=notes)


def claim_fees(signal: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    notes = f"Raydium CLMM claim_fees: position={signal.get('position_id')} pool={signal.get('pool_address')}"
    return dry_run_result("claim_fees", signal, cfg, notes=notes)
