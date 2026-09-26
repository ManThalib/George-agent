#!/usr/bin/env python3
"""George multi-DEX executor loop.

Reads signals from signals/pending/, validates them, dispatches to the correct
handler, and moves each handled signal to signals/processed/.

Modes:
    dry_run     simulate every transaction, never sign
    confirm_each simulate, then pause and ask owner for explicit approval
    auto        simulate, then sign and send automatically if all rails pass

Usage:
    python3 executor.py [--once]
    python3 executor.py --approve <signal_id>
    python3 executor.py --reject <signal_id>
    python3 executor.py --list-approvals
    python3 executor.py --check
"""

import argparse
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Tuple

from common import utc_now
from config_loader import ConfigError, load_config
from journal import append as journal_append
from rails_loader import load_rails
from signal_validator import SignalValidationError, validate, validate_dict
import tx_builder
import approvals as approvals_store


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
    # The executor itself does not know the channel; the calling agent reads
    # journal or stdout. Print a clear line so it can be picked up.
    print(f"[OWNER_NOTIFICATION] {text}")


def _summarise_simulation(sim: Dict[str, Any]) -> str:
    if isinstance(sim, list):
        return f"{len(sim)} tx(s); first ok={sim[0].get('ok') if sim else 'n/a'}"
    return f"ok={sim.get('ok')} err={sim.get('err')} units={sim.get('units_consumed')}"


def _run_simulation(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Simulate a validated signal. Returns (decision, details)."""
    try:
        sim = tx_builder.simulate(signal, cfg, rails)
    except Exception as exc:
        return "failed", {"stage": "simulate", "error": str(exc), "traceback": traceback.format_exc()}

    sim_result = sim.get("simulation") or sim
    notes = sim.get("notes", "")
    tx_base64 = sim.get("tx_base64")
    return "dry_run", {
        "simulation": sim_result,
        "tx_base64": tx_base64,
        "notes": notes,
    }


def process_signal(signal_path: Path, cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Validate a signal and act according to cfg mode."""
    try:
        signal = validate(signal_path, cfg, rails)
    except SignalValidationError as exc:
        return "rejected", {"error": str(exc), "signal_id": signal_path.stem}

    mode = cfg.get("mode", "dry_run")
    if mode == "auto":
        return _handle_auto(signal, cfg, rails)
    if mode == "confirm_each":
        return _handle_confirm_each(signal, cfg, rails)
    # dry_run / anything else
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
        f"Run: python3 executor/executor.py --approve {signal['signal_id']}"
    )
    return "awaiting_approval", {
        "simulation": sim,
        "tx_base64": details.get("tx_base64"),
        "notes": details.get("notes", ""),
    }


def _sim_ok(sim: Any) -> bool:
    if isinstance(sim, list):
        return all(s.get("ok") for s in sim)
    return bool(sim and sim.get("ok"))


def _do_send(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    try:
        result = tx_builder.send(signal, cfg, rails)
    except Exception as exc:
        return "failed", {"stage": "send", "error": str(exc), "traceback": traceback.format_exc()}

    return "executed", {"result": result}


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
        status, details = process_signal(signal_path, cfg, rails)
        signal_for_journal = None
        try:
            signal_for_journal = validate(signal_path, cfg, rails)
        except Exception:
            signal_for_journal = {"signal_id": signal_path.stem}
        journal_append(
            signal=signal_for_journal,
            decision=status,
            details=details,
            rails_hash=rails_hash_from_path(),
        )
        print(f"  {signal_path.name} -> {status}: {details.get('error') or details.get('notes', '')}")
        if status in {"rejected", "failed", "awaiting_approval", "dry_run", "executed"}:
            _move_to_processed(signal_path)


def rails_hash_from_path() -> str:
    from common import rails_hash as _rh
    from rails_loader import RAILS_PATH
    return _rh(RAILS_PATH)


def do_approve(signal_id: str, cfg: Dict[str, Any], rails: Dict[str, Any]) -> None:
    record = approvals_store.load_request(signal_id)
    if not record:
        print(f"No pending approval for {signal_id}")
        sys.exit(1)
    signal = record["signal"]
    # Re-check kill switch
    if KILL_FILE.exists():
        print(f"[{utc_now()}] KILL file present; cannot approve {signal_id}.")
        sys.exit(1)

    # Re-validate rails before sending.
    try:
        signal = validate_dict(signal, cfg, rails)
    except SignalValidationError as exc:
        print(f"Approval rejected: rails no longer pass: {exc}")
        sys.exit(1)

    decision, details = _do_send(signal, cfg, rails)
    journal_append(
        signal=signal,
        decision=decision,
        details=details,
        rails_hash=rails_hash_from_path(),
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
        rails_hash=rails_hash_from_path(),
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
    parser = argparse.ArgumentParser(description="George multi-DEX executor")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--approve", metavar="SIGNAL_ID", help="Approve and send a pending transaction")
    parser.add_argument("--reject", metavar="SIGNAL_ID", help="Reject a pending approval")
    parser.add_argument("--list-approvals", action="store_true", help="List pending approvals")
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
