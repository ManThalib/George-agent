#!/usr/bin/env python3
"""George multi-DEX executor dispatcher.

Reads signals from signals/pending/, validates them per DEX, dispatches via the
chain dispatcher, and moves each handled signal to signals/processed/.

Modes:
    dry_run     simulate every transaction, never sign
    confirm_each simulate, then pause and ask owner for explicit approval
    auto        simulate, then sign and send automatically if all rails pass

Usage:
    python3 dispatcher.py [--once]
    python3 dispatcher.py --approve <signal_id>
    python3 dispatcher.py --reject <signal_id>
    python3 dispatcher.py --list-approvals
    python3 dispatcher.py --list-dust-swaps
    python3 dispatcher.py --check
"""

import argparse
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Tuple

from common.utils import utc_now, load_json, rails_hash
from common.config_loader import ConfigError, load_config
from common.journal import append as journal_append
from common.rails_loader import RAILS_PATH, load_rails
from common.signal_validator import SignalValidationError, validate_core
from common.chain_client import ChainError, simulate as chain_simulate, send as chain_send
import common.approvals as approvals_store
import common.dust_queue as dust_queue


SIGNALS_DIR = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals")
KILL_FILE = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/KILL")


def _iter_pending() -> List[Path]:
    pending = SIGNALS_DIR / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    return sorted(pending.glob("*.json"))


def _move_to_processed(signal_path: Path) -> Path:
    processed_dir = SIGNALS_DIR / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / signal_path.name
    if dest.exists():
        dest = processed_dir / f"{signal_path.stem}-{int(time.time())}{signal_path.suffix}"
    shutil.move(str(signal_path), str(dest))
    return dest


def _notify_owner(text: str) -> None:
    print(f"[OWNER_NOTIFICATION] {text}")


def _summarise_simulation(sim: Any) -> str:
    if isinstance(sim, list):
        return f"{len(sim)} tx(s); first ok={sim[0].get('ok') if sim else 'n/a'}"
    return f"ok={sim.get('ok')} err={sim.get('err')} units={sim.get('units_consumed')}"


def _get_dex_module(dex: str):
    if dex == "meteora":
        from dexes.meteora import validator as dex_validator  # noqa: F401
        from dexes.meteora import builder as dex_builder
        from dexes.meteora.validator import validate as dex_validate
    elif dex == "orca":
        from dexes.orca import validator as dex_validator  # noqa: F401
        from dexes.orca import builder as dex_builder
        from dexes.orca.validator import validate as dex_validate
    elif dex == "raydium":
        from dexes.raydium import validator as dex_validator  # noqa: F401
        from dexes.raydium import builder as dex_builder
        from dexes.raydium.validator import validate as dex_validate
    else:
        raise ValueError(f"unsupported dex: {dex}")
    return dex_validate, dex_builder


def _build_swap_request(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "dex": "jupiter",
        "action": "swap",
        "mode": "send",
        "rpc_url": cfg["rpc_https_url"],
        "fallback_rpc_urls": cfg.get("rpc_fallback_urls", []),
        "fallback_delay_seconds": cfg.get("rpc_fallback_delay_seconds", 15),
        "wallet_public_key": cfg["wallet_public_key"],
        "max_slippage_bps": min(int(signal.get("max_slippage_bps", rails.get("max_slippage_bps", 100))), int(rails.get("max_slippage_bps", 100))),
        "priority_fee_cap_sol": rails.get("priority_fee_cap_sol", 0.0005),
        "input_mint": signal["input_mint"],
        "output_mint": signal["output_mint"],
        "amount": signal["amount"],
        "exact_out": signal.get("exact_out", False),
        "max_price_impact_pct": rails.get("max_price_impact_pct", 1.5),
    }


def _run_simulation(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    action = signal["action"]
    if action == "swap":
        req = _build_swap_request(signal, cfg, rails)
    else:
        dex = signal.get("dex") or "meteora"
        dex_validate, dex_builder = _get_dex_module(dex)
        signal = dex_validate(signal, cfg, rails)
        req = dex_builder.build(signal, cfg, rails)

    try:
        sim = chain_simulate(req)
    except ChainError as exc:
        return "failed", {"stage": "simulate", "error": str(exc)}
    except Exception as exc:
        return "failed", {"stage": "simulate", "error": str(exc), "traceback": traceback.format_exc()}

    return "dry_run", {
        "simulation": sim.get("simulation"),
        "tx_base64": sim.get("tx_base64"),
        "notes": sim.get("notes", ""),
    }


def _do_send(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    action = signal["action"]
    if action == "swap":
        req = _build_swap_request(signal, cfg, rails)
    else:
        dex = signal.get("dex") or "meteora"
        dex_validate, dex_builder = _get_dex_module(dex)
        signal = dex_validate(signal, cfg, rails)
        req = dex_builder.build(signal, cfg, rails)

    try:
        result = chain_send(req)
    except Exception as exc:
        return "failed", {"stage": "send", "error": str(exc), "traceback": traceback.format_exc()}

    details: Dict[str, Any] = {"result": result}
    for key in ("confirmed_via", "fallback_broadcast", "confirmations"):
        if isinstance(result, dict) and result.get(key) is not None:
            details[key] = result[key]
    return "executed", details


def _sim_ok(sim: Any) -> bool:
    if isinstance(sim, list):
        return all(s.get("ok") for s in sim)
    return bool(sim and sim.get("ok"))


def process_signal(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    action = signal.get("action")

    if action == "swap_to_usdc":
        path = dust_queue.queue(signal)
        _notify_owner(
            f"DUST SWAP QUEUED signal={signal['signal_id']} "
            f"mint={signal.get('mint')} symbol={signal.get('symbol')} "
            f"value_usd={signal.get('value_usd')}. "
            f"Review: python3 executor/dispatcher.py --list-dust-swaps"
        )
        return "queued_for_review", {"queue_path": str(path), "notes": f"queued swap_to_usdc for review: {signal.get('reason', '')}"}

    if action == "swap":
        signal = validate_core(signal, cfg, rails)
    else:
        dex = signal.get("dex") or "meteora"
        dex_validate, _ = _get_dex_module(dex)
        signal = dex_validate(signal, cfg, rails)

    mode = cfg.get("mode", "dry_run")
    if mode == "auto":
        return _handle_auto(signal, cfg, rails)
    if mode == "confirm_each":
        return _handle_confirm_each(signal, cfg, rails)

    decision, details = _run_simulation(signal, cfg, rails)
    return decision, details


def _handle_auto(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    decision, details = _run_simulation(signal, cfg, rails)
    if decision != "dry_run":
        return decision, details
    sim = details.get("simulation")
    if not _sim_ok(sim):
        return "rejected", {"stage": "simulate", "error": "simulation failed", "simulation": sim}
    return _do_send(signal, cfg, rails)


def _handle_confirm_each(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    decision, details = _run_simulation(signal, cfg, rails)
    if decision != "dry_run":
        return decision, details
    sim = details.get("simulation")
    if not _sim_ok(sim):
        return "rejected", {"stage": "simulate", "error": "simulation failed", "simulation": sim}

    approvals_store.store_request(signal, sim)
    _notify_owner(
        f"APPROVAL REQUIRED signal={signal['signal_id']} "
        f"action={signal.get('action')} dex={signal.get('dex')} "
        f"pool={signal.get('pool_address')}. "
        f"Run: python3 executor/dispatcher.py --approve {signal['signal_id']}"
    )
    return "awaiting_approval", {
        "simulation": sim,
        "tx_base64": details.get("tx_base64"),
        "notes": details.get("notes", ""),
    }


def run_once(cfg: Dict[str, Any], rails: Dict[str, Any]) -> None:
    if KILL_FILE.exists():
        print(f"[{utc_now()}] KILL file present; halting.")
        return

    pending = _iter_pending()
    if not pending:
        print(f"[{utc_now()}] No pending signals.")
        return

    print(f"[{utc_now()}] Processing {len(pending)} signal(s)...")
    for signal_path in pending:
        status, details = process_signal_path(signal_path, cfg, rails)
        print(f"  {signal_path.name} -> {status}: {details.get('error') or details.get('notes', '')}")
        if status in {"rejected", "failed", "awaiting_approval", "dry_run", "executed", "queued_for_review"}:
            _move_to_processed(signal_path)


def process_signal_path(signal_path: Path, cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    try:
        signal = load_json(signal_path)
    except Exception as exc:
        return "rejected", {"error": f"cannot read signal: {exc}"}

    signal_id = signal.get("signal_id", signal_path.stem)
    signal["signal_id"] = signal_id

    try:
        status, details = process_signal(signal, cfg, rails)
    except SignalValidationError as exc:
        status, details = "rejected", {"error": str(exc), "signal_id": signal_id}
    except Exception as exc:
        status, details = "failed", {"error": str(exc), "traceback": traceback.format_exc()}

    journal_append(
        signal=signal,
        decision=status,
        details=details,
        rails_hash=rails_hash(RAILS_PATH),
    )
    return status, details


def do_approve(signal_id: str, cfg: Dict[str, Any], rails: Dict[str, Any]) -> None:
    record = approvals_store.load_request(signal_id)
    if not record:
        print(f"No pending approval for {signal_id}")
        sys.exit(1)
    signal = record["signal"]

    if KILL_FILE.exists():
        print(f"[{utc_now()}] KILL file present; cannot approve {signal_id}.")
        sys.exit(1)

    action = signal.get("action")
    try:
        if action == "swap":
            signal = validate_core(signal, cfg, rails)
        else:
            dex = signal.get("dex") or "meteora"
            dex_validate, _ = _get_dex_module(dex)
            signal = dex_validate(signal, cfg, rails)
    except SignalValidationError as exc:
        print(f"Approval rejected: rails no longer pass: {exc}")
        sys.exit(1)

    decision, details = _do_send(signal, cfg, rails)
    journal_append(
        signal=signal,
        decision=decision,
        details=details,
        rails_hash=rails_hash(RAILS_PATH),
    )
    print(f"{signal_id} -> {decision}: {details}")
    if decision == "executed":
        approvals_store.move_to(signal_id, approvals_store.APPROVED_DIR)
    else:
        approvals_store.move_to(signal_id, approvals_store.REJECTED_DIR)


def do_reject(signal_id: str, cfg: Dict[str, Any], rails: Dict[str, Any]) -> None:
    record = approvals_store.load_request(signal_id)
    if not record:
        print(f"No pending approval for {signal_id}")
        sys.exit(1)
    approvals_store.move_to(signal_id, approvals_store.REJECTED_DIR)
    journal_append(
        signal=record["signal"],
        decision="rejected_by_owner",
        details={"reason": "owner rejected approval"},
        rails_hash=rails_hash(RAILS_PATH),
    )
    print(f"{signal_id} -> rejected by owner")


def do_list_approvals() -> None:
    pending = approvals_store.list_pending()
    if not pending:
        print("No pending approvals.")
        return
    print(f"{'Signal ID':<40} {'Action':<8} {'DEX':<10} Pool")
    for sid, rec in pending.items():
        sig = rec.get("signal", {})
        print(f"{sid:<40} {sig.get('action',''):<8} {sig.get('dex',''):<10} {sig.get('pool_address','')}")


def do_list_dust_swaps() -> None:
    records = dust_queue.list_pending()
    if not records:
        print("No dust swaps pending review.")
        return
    print(f"{'Signal ID':<40} {'Symbol':<10} {'Value USD':<10} Reason")
    for rec in records:
        sig = rec.get("signal", {})
        print(
            f"{rec.get('signal_id', ''):<40} {sig.get('symbol', ''):<10} "
            f"{str(sig.get('value_usd', '')):<10} {sig.get('reason', '')}"
        )


def do_check(cfg: Dict[str, Any]) -> None:
    print("Executor health check")
    print(f"  mode: {cfg.get('mode')}")
    print(f"  wallet: {cfg.get('wallet_public_key')}")
    print(f"  rpc: {cfg.get('rpc_https_url', '')[:60]}...")
    missing = []
    if not os.environ.get("SOLANA_PUBLIC_WALLET") and not cfg.get("wallet_public_key"):
        missing.append("SOLANA_PUBLIC_WALLET")
    if not os.environ.get("SOLANA_RPC_URL") and not cfg.get("rpc_https_url"):
        missing.append("SOLANA_RPC_URL")
    if missing:
        print(f"  MISSING env/config: {', '.join(missing)}")
    else:
        print("  env: ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="George multi-DEX executor dispatcher")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--approve", metavar="SIGNAL_ID", help="Approve and send a pending transaction")
    parser.add_argument("--reject", metavar="SIGNAL_ID", help="Reject a pending approval")
    parser.add_argument("--list-approvals", action="store_true", help="List pending approvals")
    parser.add_argument("--list-dust-swaps", action="store_true", help="List swap_to_usdc signals pending review")
    parser.add_argument("--check", action="store_true", help="Health check")
    args = parser.parse_args()

    cfg = load_config()
    rails = load_rails()

    if args.approve:
        do_approve(args.approve, cfg, rails)
        return
    if args.reject:
        do_reject(args.reject, cfg, rails)
        return
    if args.list_approvals:
        do_list_approvals()
        return
    if args.list_dust_swaps:
        do_list_dust_swaps()
        return
    if args.check:
        do_check(cfg)
        return

    if args.once:
        run_once(cfg, rails)
        return

    while True:
        run_once(cfg, rails)
        time.sleep(cfg.get("heartbeat_interval_seconds", 60))


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)
