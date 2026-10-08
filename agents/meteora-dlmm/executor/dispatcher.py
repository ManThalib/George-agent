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
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.utils import utc_now, load_json, rails_hash
from common.config_loader import ConfigError, load_config
from common.journal import append as journal_append
from common.rails_loader import RAILS_PATH, load_rails
from common.signal_validator import SignalValidationError, validate_core
from common.chain_client import ChainError, simulate as chain_simulate, send as chain_send
from common import mirror
import common.approvals as approvals_store
import common.dust_queue as dust_queue
import common.exposure_guard as exposure_guard

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ledger"))
import ledger as trade_ledger  # noqa: E402  (executor-local ledger module)


SIGNALS_DIR = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals")
FAILED_VERIFY_DIR = SIGNALS_DIR / "failed_verify"
KILL_FILE = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/KILL")

# --- Capital refresh chain (swap -> wallet rescan -> Sheldon re-entry) ---
# After a successful prep swap the wallet changed; Missy's wallet screener
# re-reads balances and Sheldon re-evaluates (emitting the open when funded).
WALLET_REFRESH_SCRIPT = "/data/missy-agent/wallet_screener/run_wallet.sh"
WALLET_SCAN_DIR = "/data/missy-data/wallet_screens"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SHELDON_CYCLE_CMD = ["python3", "/data/.openclaw/workspace-agents/sheldon/scoring/run_cycle.py", "--write-signals"]
REENTRY_STATE_PATH = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/state/capital_reentry.json")
LOCK_PATH = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/state/dispatcher.lock")
MAX_CAPITAL_REENTRIES_PER_HOUR = 8
# A prep swap re-enters the dispatcher after Sheldon re-scores (to process
# the open, or another prep swap). Depth bounds the in-process chain; the
# hourly re-entry guard above is the outer backstop.
MAX_CAPITAL_CHAIN_DEPTH = 3

# Prep swaps processed per run_once wake. A deferred-doorbell backlog used to
# let several preps execute at once (seen live 2026-10-05 15:48: a double buy
# plus a sell-back). Only preps are capped; closes and claims are never
# deferred. Override with GEORGE_MAX_PREPS_PER_RUN.
DEFAULT_MAX_PREPS_PER_RUN = 1

# Sheldon prep swap signals are named sheldon-prep-<base>-<idx>.json.
PREP_SIGNAL_PREFIX = "sheldon-prep-"


def _max_preps_per_run() -> int:
    try:
        return max(0, int(os.environ.get("GEORGE_MAX_PREPS_PER_RUN",
                                         str(DEFAULT_MAX_PREPS_PER_RUN))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PREPS_PER_RUN


def _is_prep_signal_path(signal_path: Path) -> bool:
    return signal_path.name.startswith(PREP_SIGNAL_PREFIX)


@contextmanager
def _dispatcher_lock(timeout_seconds: int = 600):
    """Serialize dispatcher runs: pending signals must never be processed
    concurrently, or the same swap executes twice (seen live 2026-09-29:
    nested refresh follow-ups re-executed one prep swap three times)."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    deadline = time.time() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f"dispatcher lock not free after {timeout_seconds}s")
                time.sleep(1)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _record_execution_state(signal: Dict[str, Any]) -> None:
    """Update persistent execution state after a successful on-chain tx.

    Wrapped by the callers so a guard failure can never mask an executed tx.
    """
    action = signal.get("action")
    if action == "open":
        exposure_guard.record_open_executed(signal)
    elif action in {"close"}:
        exposure_guard.record_close_executed(signal)
    elif action in exposure_guard.CLAIM_ACTIONS:
        # A fee claim is not a close: it must not remove the position.
        exposure_guard.record_claim_executed(signal)
    elif action == "add_liquidity":
        exposure_guard.record_add_executed(signal)
    elif action == "remove_liquidity":
        exposure_guard.record_remove_executed(signal)
    elif action == "rebalance":
        exposure_guard.record_close_executed(signal)
        exposure_guard.record_open_executed(signal)
    elif action == "rotate":
        exposure_guard.record_close_executed(signal)
        exposure_guard.record_open_executed(signal)


def _merge_execution_result(signal: Dict[str, Any],
                            details: Dict[str, Any]) -> Dict[str, Any]:
    """Copy execution-derived identifiers onto the signal before recording state.

    The signal file describes the *request*; the on-chain position id only
    exists after the DEX handler runs (`details["result"]["position_id"]`).
    Recording the raw signal file stored `position_id: None` for every
    executed open, which silently disabled position-id matching and the
    mutation cooldown rail (both no-op on a falsy position_id).
    """
    result = details.get("result") if isinstance(details, dict) else None
    if isinstance(result, dict):
        for key in ("position_id", "dex"):
            value = result.get(key)
            if value and not signal.get(key):
                signal[key] = value
    if not signal.get("wallet_id"):
        signal["wallet_id"] = "main"
    return signal


def _safe_record_execution_state(signal: Dict[str, Any],
                                 details: Optional[Dict[str, Any]] = None) -> None:
    """Record execution state; never let a guard failure mask the result."""
    try:
        if details is not None:
            signal = _merge_execution_result(signal, details)
        _record_execution_state(signal)
    except Exception as exc:
        print(f"[{utc_now()}] exposure state record failed: {exc}")


def _reconcile_exposure() -> None:
    """Add on-chain positions missing from the tracked exposure state.

    Runs once per run_once. Additive and conservative (see
    exposure_guard.reconcile_positions); a stale scan is skipped. Never
    raises: reconciliation is a safety net, not a gate.
    """
    try:
        positions, mtime = exposure_guard.load_scan_positions()
        report = exposure_guard.reconcile_positions(positions, scan_mtime=mtime)
        if report.get("skipped"):
            print(f"[{utc_now()}] exposure reconcile skipped: {report['skipped']}")
            return
        for entry in report.get("added") or []:
            print(f"[{utc_now()}] exposure reconcile added "
                  f"{entry.get('pool_address')} (wallet {entry.get('wallet_id')}, "
                  f"${entry.get('position_usd')})")
        for entry in report.get("unmatched") or []:
            print(f"[{utc_now()}] exposure reconcile: tracked position "
                  f"{entry.get('pool_address')} absent from scan (kept; needs a "
                  f"confirmed close to remove)")
    except Exception as exc:
        print(f"[{utc_now()}] exposure reconcile failed: {exc}")


def _reentry_window(state: Dict[str, Any], now: float) -> List[float]:
    """Timestamps of capital re-entries inside the last hour."""
    stamps = state.get("timestamps") or []
    return [t for t in stamps if isinstance(t, (int, float)) and now - t < 3600]


def _reentry_allowed(state: Dict[str, Any], now: float) -> Tuple[bool, str]:
    recent = _reentry_window(state, now)
    if len(recent) >= MAX_CAPITAL_REENTRIES_PER_HOUR:
        return False, f"loop guard: {len(recent)} capital re-entries in the last hour"
    return True, ""


def _load_reentry_state() -> Dict[str, Any]:
    try:
        with open(REENTRY_STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {"timestamps": []}


def _save_reentry_state(state: Dict[str, Any]) -> None:
    try:
        REENTRY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(REENTRY_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except Exception as exc:
        print(f"[{utc_now()}] capital re-entry state save failed: {exc}")


def _newest_wallet_scan_path() -> Optional[str]:
    """Newest wallet scan by mtime (not filename).

    Sorting by filename always resolves wallet_screen-latest.json, so a
    refresh that wrote a new timestamped file but did not (yet) move the
    symlink would be invisible. mtime sees the real newest scan.
    """
    if not os.path.isdir(WALLET_SCAN_DIR):
        return None
    scans = [
        os.path.join(WALLET_SCAN_DIR, p) for p in os.listdir(WALLET_SCAN_DIR)
        if p.startswith("wallet_screen-") and p.endswith(".json")
        and not p.endswith((".failed", ".invalid"))
    ]
    if not scans:
        return None
    return max(scans, key=os.path.getmtime)


def _read_usdc_scan() -> Tuple[Optional[int], Optional[str], float]:
    """(USDC raw balance, scan path, scan mtime) from the newest scan by mtime."""
    path = _newest_wallet_scan_path()
    if not path:
        return None, None, 0.0
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None, path, mtime
    for a in data.get("assets", []):
        if a.get("mint") == USDC_MINT:
            try:
                return int(a.get("amount_raw") or 0), path, mtime
            except (TypeError, ValueError):
                return None, path, mtime
    return 0, path, mtime


def _read_latest_usdc_raw() -> Optional[int]:
    """USDC raw balance from the newest wallet scan (verification only)."""
    return _read_usdc_scan()[0]


def _run_wallet_refresh(cfg: Dict[str, Any], expected_usdc_delta: int = 0,
                        ambiguous: bool = False) -> str:
    rpc = cfg.get("rpc_https_url")
    wallet = cfg.get("wallet_public_key")
    if not rpc or not wallet:
        return "skipped (missing rpc_https_url / wallet_public_key in config)"
    env = {**os.environ, "SOLANA_RPC_URL": rpc, "WALLET_PUBLIC_KEY": wallet}
    pre, pre_path, pre_mtime = _read_usdc_scan()
    last_out = ""
    cur: Optional[int] = pre
    for attempt in range(4):
        try:
            proc = subprocess.run(
                ["bash", WALLET_REFRESH_SCRIPT], env=env, capture_output=True,
                text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "FAILED (timeout after 120s)"
        except Exception as exc:
            return f"FAILED ({exc})"
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
            return f"FAILED exit={proc.returncode}: {tail[0][:160]}"
        last_out = (proc.stdout or "").strip().splitlines()[-1:]
        cur, cur_path, cur_mtime = _read_usdc_scan()
        if cur is None or pre is None:
            return f"ok -> {last_out[0] if last_out else ''}"
        # A refresh that did not produce a new scan cannot verify anything:
        # reporting "delta unverified" here was a false negative.
        if cur_path == pre_path and cur_mtime <= pre_mtime:
            if attempt < 3:
                time.sleep(6)  # let the refresh land a new scan
                continue
            name = os.path.basename(pre_path or "scan")
            return (f"ok (scan not refreshed: {name} unchanged) "
                    f"-> {last_out[0] if last_out else ''}")
        # A new scan landed. Only now can the USDC delta be asserted.
        if ambiguous:
            return (f"ok (usdc {pre} -> {cur}; ambiguous batch, delta not asserted) "
                    f"-> {last_out[0] if last_out else ''}")
        if expected_usdc_delta < 0 and cur <= pre + int(expected_usdc_delta * 0.9):
            return f"ok (usdc {pre} -> {cur}) -> {last_out[0] if last_out else ''}"
        if expected_usdc_delta > 0 and cur >= pre + int(expected_usdc_delta * 0.9):
            return f"ok (usdc {pre} -> {cur}) -> {last_out[0] if last_out else ''}"
        if expected_usdc_delta == 0:
            return f"ok -> {last_out[0] if last_out else ''}"
        if attempt < 3:
            time.sleep(6)  # let the RPC indexer catch up with the confirmed tx
    return (f"ok (usdc delta unverified: {pre} -> {cur}) "
            f"-> {last_out[0] if last_out else ''}")


def _run_sheldon_reentry() -> Tuple[str, str]:
    try:
        proc = subprocess.run(
            SHELDON_CYCLE_CMD, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return "FAILED (timeout after 300s)", ""
    except Exception as exc:
        return f"FAILED ({exc})", ""
    tail = (proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
    if proc.returncode != 0:
        err_tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return f"FAILED exit={proc.returncode}: {err_tail[0][:160]}", proc.stdout or ""
    return f"ok: {tail[0][:160]}", proc.stdout or ""


def _run_dispatcher_followup(depth: int) -> str:
    """Process the signals Sheldon just wrote (open, or another prep swap)."""
    env = {**os.environ, "GEORGE_CAPITAL_DEPTH": str(depth + 1)}
    try:
        proc = subprocess.run(
            ["python3", str(Path(__file__).resolve()), "--once"],
            capture_output=True, text=True, timeout=600, env=env,
        )
    except subprocess.TimeoutExpired:
        return "FAILED (timeout after 600s)"
    except Exception as exc:
        return f"FAILED ({exc})"
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    tail = lines[-1][:200] if lines else "no output"
    if proc.returncode != 0:
        return f"FAILED exit={proc.returncode}: {tail}"
    return f"ok: {tail}"


def _trigger_capital_refresh(cfg: Dict[str, Any], signal_id: str = "",
                             expected_usdc_delta: int = 0,
                             ambiguous: bool = False) -> None:
    """After a successful swap: refresh Missy's wallet scan, re-run
    Sheldon's cycle, and process whatever signals it wrote (the open, or
    another prep swap). This closes the capital-prep loop inside one
    dispatcher invocation.

    Failures here never undo the swap; they only cost latency (the hourly
    pipeline re-runs Sheldon anyway).
    """
    if os.environ.get("GEORGE_SKIP_CAPITAL_REFRESH"):
        print(f"[{utc_now()}] capital refresh skipped (GEORGE_SKIP_CAPITAL_REFRESH set)")
        return
    if KILL_FILE.exists():
        print(f"[{utc_now()}] capital refresh skipped (KILL file present)")
        return
    depth = int(os.environ.get("GEORGE_CAPITAL_DEPTH") or 0)
    if depth >= MAX_CAPITAL_CHAIN_DEPTH:
        _notify_owner(
            f"CAPITAL CHAIN DEPTH LIMIT signal={signal_id}: depth {depth} >= "
            f"{MAX_CAPITAL_CHAIN_DEPTH}; remaining legs left to the next cycle"
        )
        return

    now = time.time()
    state = _load_reentry_state()
    allowed, reason = _reentry_allowed(state, now)
    if not allowed:
        _notify_owner(f"CAPITAL REFRESH SKIPPED signal={signal_id}: {reason}")
        return
    state["timestamps"] = _reentry_window(state, now) + [now]
    _save_reentry_state(state)

    print(f"[{utc_now()}] capital refresh for {signal_id} (depth {depth}): wallet rescan...")
    wallet_result = _run_wallet_refresh(cfg, expected_usdc_delta, ambiguous)
    print(f"[{utc_now()}] wallet rescan: {wallet_result}")

    print(f"[{utc_now()}] capital refresh: Sheldon re-entry...")
    sheldon_result, sheldon_stdout = _run_sheldon_reentry()
    print(f"[{utc_now()}] Sheldon re-entry: {sheldon_result}")

    followup_result = "skipped (no new signals)"
    if "WAKE_GEORGE:" in sheldon_stdout:
        print(f"[{utc_now()}] capital refresh: dispatcher follow-up...")
        followup_result = _run_dispatcher_followup(depth)
        print(f"[{utc_now()}] dispatcher follow-up: {followup_result}")

    _notify_owner(
        f"CAPITAL REFRESH signal={signal_id}: wallet rescan {wallet_result}; "
        f"Sheldon {sheldon_result}; follow-up {followup_result}"
    )


def _iter_pending() -> List[Path]:
    pending = SIGNALS_DIR / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    return sorted(pending.glob("*.json"))


def _move_to_processed(signal_path: Path) -> Path:
    processed_dir = SIGNALS_DIR / "processed"
    return _move_to_dir(signal_path, processed_dir)


def _move_to_dir(signal_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / signal_path.name
    if dest.exists():
        dest = dest_dir / f"{signal_path.stem}-{int(time.time())}{signal_path.suffix}"
    shutil.move(str(signal_path), str(dest))
    return dest


def _notify_owner(text: str) -> None:
    print(f"[OWNER_NOTIFICATION] {text}")


def _now_utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _self_verify_block(details: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The verify block written by the per-DEX chain handler, if any."""
    result = details.get("result")
    if isinstance(result, dict) and isinstance(result.get("verify"), dict):
        return result["verify"]
    return None


def _handle_self_verify(signal: Dict[str, Any], signal_path: Path,
                        details: Dict[str, Any]) -> Path:
    """Route an executed signal based on its on-chain self-verification.

    Opens that fail (or lack) verification go to signals/failed_verify/
    instead of processed/, with an owner notification. Closes are only
    flagged: the execution already happened and is journaled as executed.
    Returns the directory the signal file landed in.
    """
    action = signal.get("action")
    verify = _self_verify_block(details)
    if action in {"open", "add_liquidity"} and (verify is None or not verify.get("ok")):
        dest = _move_to_dir(signal_path, FAILED_VERIFY_DIR)
        _notify_owner(
            f"SELF-VERIFY FAILED signal={signal.get('signal_id')} action={action} "
            f"dex={signal.get('dex')} pool={signal.get('pool_address')} "
            f"verify={json.dumps(verify, default=str)}; moved to {dest}"
        )
        return dest
    if action == "close" and (verify is None or not verify.get("ok")):
        _notify_owner(
            f"SELF-VERIFY WARNING signal={signal.get('signal_id')} action=close "
            f"dex={signal.get('dex')} pool={signal.get('pool_address')} "
            f"verify={json.dumps(verify, default=str)}; position may remain on-chain"
        )
    return _move_to_processed(signal_path)


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
    """Simulate a signal that has already passed validation.

    `process_signal()` is responsible for the one validation call; callers
    here must receive a validated signal and never re-validate.
    """
    action = signal["action"]
    if action == "swap":
        req = _build_swap_request(signal, cfg, rails)
    else:
        dex = signal.get("dex") or "meteora"
        _dex_validate, dex_builder = _get_dex_module(dex)
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
    """Send a signal that has already passed validation.

    `process_signal()` is responsible for the one validation call; callers
    here must receive a validated signal and never re-validate.

    Multi-wallet mirror: the incoming signal IS the MAIN leg. After it
    completes, each registered mirror runs the same decision with its own
    keypair and scaled capital. Mirror OPENs are gated on MAIN success;
    mirror CLOSE/CLAIMs run regardless (de-risking both wallets) with any
    divergence flagged as drift.
    """
    action = signal["action"]
    if action == "swap":
        req = _build_swap_request(signal, cfg, rails)
    else:
        dex = signal.get("dex") or "meteora"
        _dex_validate, dex_builder = _get_dex_module(dex)
        req = dex_builder.build(signal, cfg, rails)

    # Multi-wallet registry: resolve the wallet_id to its registered public
    # key. A mirror signal carrying an unregistered id (or a key mismatch)
    # is rejected before any chain call — fail closed.
    wallet_id = req.get("wallet_id") or "main"
    registry = cfg.get("wallet_registry") or {"main": cfg["wallet_public_key"]}
    registered = registry.get(wallet_id)
    if not registered:
        return "rejected", {
            "stage": "wallet_registry",
            "error": f"wallet_id '{wallet_id}' not registered in agent.config.json wallet.mirrors",
        }
    req["wallet_public_key"] = registered

    try:
        result = chain_send(req)
    except Exception as exc:
        return "failed", {"stage": "send", "error": str(exc), "traceback": traceback.format_exc()}

    # A gated claim reports computed pending + skip reason instead of a tx.
    # Not a failure: the rail worked. Journal it as executed-with-skip so the
    # signal moves out of pending (an under-threshold claim must not retry
    # every cycle).
    if isinstance(result, dict) and result.get("skipped"):
        details: Dict[str, Any] = {
            "result": result,
            "skip_reason": result.get("skip_reason"),
            "pending_usd": (result.get("pending_usd") or {}).get("total_usd"),
        }
        # MAIN claim skipped by the rail: mirrors would claim nothing either,
        # but record the divergence for the epilogue report.
        mirror.record_leg(signal.get("signal_id", ""), wallet_id, action,
                          "skipped", str(result.get("skip_reason", "")))
        return "skipped", details

    details: Dict[str, Any] = {"result": result}
    for key in ("confirmed_via", "fallback_broadcast", "confirmations"):
        if isinstance(result, dict) and result.get(key) is not None:
            details[key] = result[key]

    mirror_legs = _run_mirror_legs(signal, wallet_id, main_status="executed", cfg=cfg, rails=rails)
    if mirror_legs:
        details["mirror_legs"] = mirror_legs
    return "executed", details


def _run_mirror_legs(
    signal: Dict[str, Any],
    main_wallet_id: str,
    main_status: str,
    cfg: Dict[str, Any],
    rails: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Run the mirror legs for a completed MAIN signal.

    Gating (owner-approved design): mirror OPEN/add runs only when MAIN's
    leg succeeded; mirror CLOSE/CLAIM runs regardless — de-risking a mirror
    must not depend on MAIN's leg landing. Mirror failures on opens wait
    for the next cycle + drift-flag; closes are drift-flagged for owner
    attention. Zero capital decisions happen here — the leg is a structural
    copy with scaled amounts.
    """
    if main_wallet_id != "main":
        return []  # a mirror leg never spawns further mirrors
    registry = cfg.get("wallet_registry") or {}
    mirrors = mirror.mirror_wallet_ids(registry)
    if not mirrors:
        return []
    if signal.get("action") not in mirror.MIRRORABLE_ACTIONS:
        return []

    capital = float(cfg.get("mirror_capital_fraction", 1.0) or 1.0)
    legs: List[Dict[str, Any]] = []
    gate_opens = main_status == "executed"
    for wallet_id in mirrors:
        leg_signal = mirror.build_mirror_signal(signal, wallet_id, capital)
        if not leg_signal:
            continue
        open_like = leg_signal["action"] in {"open", "add_liquidity"}
        if open_like and not gate_opens:
            mirror.record_leg(
                signal.get("signal_id", ""), wallet_id, leg_signal["action"],
                "gated", f"main leg {main_status}; mirror open waits for next cycle",
            )
            mirror.record_drift(
                signal.get("signal_id", ""), wallet_id, leg_signal["action"],
                f"main leg {main_status}: mirror open deferred (next cycle decides)",
            )
            legs.append({"wallet_id": wallet_id, "status": "gated",
                         "detail": "main leg not executed; open deferred"})
            continue

        _dex_validate, dex_builder = _get_dex_module(leg_signal.get("dex") or "meteora")
        try:
            leg_req = dex_builder.build(leg_signal, cfg, rails)
            registered = registry.get(wallet_id)
            leg_req["wallet_public_key"] = registered or leg_req["wallet_public_key"]
            leg_result = chain_send(leg_req)
            if isinstance(leg_result, dict) and leg_result.get("skipped"):
                mirror.record_leg(signal.get("signal_id", ""), wallet_id,
                                  leg_signal["action"], "skipped",
                                  str(leg_result.get("skip_reason", "")))
                legs.append({"wallet_id": wallet_id, "status": "skipped",
                             "detail": leg_result.get("skip_reason")})
                continue
            mirror.record_leg(signal.get("signal_id", ""), wallet_id,
                              leg_signal["action"], "executed")
            legs.append({"wallet_id": wallet_id, "status": "executed"})
        except Exception as exc:
            mirror.record_leg(signal.get("signal_id", ""), wallet_id,
                              leg_signal["action"], "failed", str(exc))
            mirror.record_drift(signal.get("signal_id", ""), wallet_id,
                                leg_signal["action"], f"leg failed: {exc}")
            legs.append({"wallet_id": wallet_id, "status": "failed", "detail": str(exc)[:200]})
    return legs


def _sim_ok(sim: Any) -> bool:
    if isinstance(sim, list):
        return all(s.get("ok") for s in sim)
    return bool(sim and sim.get("ok"))


def _build_dust_swap_request(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Build a Jupiter swap request from a swap_to_usdc dust signal."""
    return {
        "dex": "jupiter",
        "action": "swap",
        "mode": "send",
        "rpc_url": cfg["rpc_https_url"],
        "fallback_rpc_urls": cfg.get("rpc_fallback_urls", []),
        "fallback_delay_seconds": cfg.get("rpc_fallback_delay_seconds", 15),
        "wallet_public_key": cfg["wallet_public_key"],
        "max_slippage_bps": min(
            int(signal.get("max_slippage_bps", rails.get("max_slippage_bps", 100))),
            int(rails.get("max_slippage_bps", 100)),
        ),
        "priority_fee_cap_sol": rails.get("priority_fee_cap_sol", 0.0005),
        "input_mint": signal["mint"],
        "output_mint": USDC_MINT,
        "amount": int(signal["amount_raw"]),
        "exact_out": signal.get("exact_out", False),
        "max_price_impact_pct": rails.get("max_price_impact_pct", 1.5),
    }


def _handle_swap_to_usdc(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Auto-execute a dust swap to USDC when the executor is in auto mode."""
    try:
        req = _build_dust_swap_request(signal, cfg, rails)
    except (KeyError, TypeError, ValueError) as exc:
        return "rejected", {"stage": "dust_swap_build", "error": str(exc)}

    try:
        sim = chain_simulate(req)
    except ChainError as exc:
        return "failed", {"stage": "simulate", "error": str(exc)}
    except Exception as exc:
        return "failed", {"stage": "simulate", "error": str(exc), "traceback": traceback.format_exc()}

    if not _sim_ok(sim):
        return "rejected", {"stage": "simulate", "error": "simulation failed", "simulation": sim.get("simulation")}

    try:
        result = chain_send(req)
    except ChainError as exc:
        return "failed", {"stage": "send", "error": str(exc)}
    except Exception as exc:
        return "failed", {"stage": "send", "error": str(exc), "traceback": traceback.format_exc()}

    details: Dict[str, Any] = {"result": result}
    for key in ("confirmed_via", "fallback_broadcast", "confirmations"):
        if isinstance(result, dict) and result.get(key) is not None:
            details[key] = result[key]
    return "executed", details


def process_signal(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    action = signal.get("action")

    if action == "swap_to_usdc":
        if cfg.get("mode") == "auto":
            return _handle_swap_to_usdc(signal, cfg, rails)
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

    # Exposure / loss / drawdown guard: new opens and adds deploy capital;
    # removes are de-risking and always pass.
    if action == "open":
        allowed, reason = exposure_guard.check_open_allowed(signal, rails)
        if not allowed:
            return "rejected", {"stage": "exposure_guard", "error": reason}
    elif action == "add_liquidity":
        allowed, reason = exposure_guard.check_add_allowed(signal, rails)
        if not allowed:
            return "rejected", {"stage": "exposure_guard", "error": reason}
    elif action == "remove_liquidity":
        allowed, reason = exposure_guard.check_remove_allowed(signal, rails)
        if not allowed:
            return "rejected", {"stage": "exposure_guard", "error": reason}
    elif action == "rebalance":
        allowed, reason = exposure_guard.check_rebalance_allowed(signal, rails)
        if not allowed:
            return "rejected", {"stage": "exposure_guard", "error": reason}
    elif action == "rotate":
        allowed, reason = exposure_guard.check_rotate_allowed(signal, rails)
        if not allowed:
            return "rejected", {"stage": "exposure_guard", "error": reason}

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
    # Capital refresh runs at the run_once epilogue (after the dispatcher
    # lock releases), never inline: an inline follow-up would race this
    # process's own pending loop.
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


def _expected_usdc_delta(executed: List[Dict[str, Any]]) -> Tuple[int, bool]:
    """Net USDC raw-balance change the executed swaps should produce.

    Buys (USDC in) are exact; sells (USDC out) are route-dependent, so any
    sell marks the expectation ambiguous and the refresh accepts any change.
    """
    total = 0
    ambiguous = False
    for sig in executed:
        if sig.get("action") != "swap":
            continue
        try:
            amt = int(sig.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if sig.get("input_mint") == USDC_MINT:
            total -= amt
        elif sig.get("output_mint") == USDC_MINT:
            ambiguous = True
    return total, ambiguous


def _capital_epilogue(cfg: Dict[str, Any], executed: List[Dict[str, Any]]) -> None:
    """After run_once releases the dispatcher lock: mirror-drift report, then
    (if a swap executed) the wallet rescan -> Sheldon re-entry chain."""
    drift = mirror.check_drift()
    if drift:
        lines = "; ".join(
            f"{d['signal_id']} {d['action']} wallet={d['wallet_id']}: {d['reason']}"
            for d in drift[:5]
        )
        print(f"[{utc_now()}] MIRROR DRIFT ({len(drift)}): {lines}")
        _notify_owner(
            f"MIRROR DRIFT ({len(drift)} unresolved): {lines}. "
            "Mirror wallets diverged from MAIN — inspect journal/state/mirror_sync.json; "
            "mirror opens auto-retry next cycle, closes need attention."
        )

    swaps = [s for s in executed if s.get("action") == "swap"]
    if not swaps:
        return
    expected, ambiguous = _expected_usdc_delta(swaps)
    ids = ",".join(str(s.get("signal_id")) for s in swaps)
    _trigger_capital_refresh(cfg, ids, expected, ambiguous)


def run_once(cfg: Dict[str, Any], rails: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Process all pending signals once under the dispatcher lock. Returns
    the signals that were executed on-chain (the caller decides on any
    capital-refresh chain)."""
    executed: List[Dict[str, Any]] = []
    with _dispatcher_lock():
        if KILL_FILE.exists():
            print(f"[{utc_now()}] KILL file present; halting.")
            return executed

        # Keep exposure state aligned with the authoritative on-chain scan
        # before evaluating any new signal (additive; stale scan is skipped).
        _reconcile_exposure()

        pending = _iter_pending()
        if not pending:
            print(f"[{utc_now()}] No pending signals.")
            return executed

        print(f"[{utc_now()}] Processing {len(pending)} signal(s)...")
        max_preps = _max_preps_per_run()
        preps_done = 0
        deferred_preps: List[str] = []
        for signal_path in pending:
            # Bound prep swaps per wake: a doorbell backlog must not fire
            # several preps at once. Closes/claims are never deferred.
            if _is_prep_signal_path(signal_path):
                if preps_done >= max_preps:
                    deferred_preps.append(signal_path.name)
                    continue
                preps_done += 1
            status, details = process_signal_path(signal_path, cfg, rails)
            print(f"  {signal_path.name} -> {status}: {details.get('error') or details.get('notes', '')}")
            if status in {"rejected", "failed", "awaiting_approval", "dry_run", "executed", "queued_for_review", "skipped"}:
                if status == "executed":
                    try:
                        executed_signal = load_json(signal_path)
                    except Exception:
                        executed_signal = {"signal_id": signal_path.stem}
                    landed = _handle_self_verify(executed_signal, signal_path, details)
                    readback = landed
                else:
                    _move_to_processed(signal_path)
                    readback = _processed_path(signal_path)
            if status == "executed":
                try:
                    processed_signal = load_json(readback)
                except Exception:
                    processed_signal = {"signal_id": signal_path.stem}
                executed.append(processed_signal)
                _safe_record_execution_state(processed_signal, details)
        if deferred_preps:
            print(f"[{utc_now()}] prep cap {max_preps}/wake: deferred "
                  f"{len(deferred_preps)} prep signal(s) to the next wake: "
                  f"{', '.join(deferred_preps)}")
    return executed


def _processed_path(signal_path: Path) -> Path:
    return SIGNALS_DIR / "processed" / signal_path.name


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
    if status == "executed":
        try:
            trade_ledger.append_row(
                trade_ledger.build_row(signal, status, details, _now_utc_iso())
            )
        except Exception as exc:
            # A ledger failure must never mask the execution result.
            print(f"[{utc_now()}] ledger row append failed: {exc}")
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
    if decision == "executed":
        try:
            trade_ledger.append_row(
                trade_ledger.build_row(signal, decision, details, _now_utc_iso())
            )
        except Exception as exc:
            print(f"[{utc_now()}] ledger row append failed: {exc}")
    if decision == "executed" and action == "swap":
        try:
            amt = int(signal.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0
        expected = -amt if signal.get("input_mint") == USDC_MINT else 0
        ambiguous = signal.get("output_mint") == USDC_MINT
        _trigger_capital_refresh(cfg, signal_id, expected, ambiguous)
    if decision == "executed":
        # The approval path must update exposure state too, not just run_once.
        _safe_record_execution_state(signal, details)
    print(f"{signal_id} -> {decision}: {details}")
    if decision == "executed":
        verify = _self_verify_block(details)
        if action == "open" and (verify is None or not verify.get("ok")):
            _notify_owner(
                f"SELF-VERIFY FAILED signal={signal_id} action=open "
                f"dex={signal.get('dex')} pool={signal.get('pool_address')} "
                f"verify={json.dumps(verify, default=str)}; approval moved to "
                f"failed_verify/ for review"
            )
            approvals_store.move_to(signal_id, approvals_store.FAILED_VERIFY_DIR)
        elif action == "close" and (verify is None or not verify.get("ok")):
            _notify_owner(
                f"SELF-VERIFY WARNING signal={signal_id} action=close "
                f"dex={signal.get('dex')} pool={signal.get('pool_address')} "
                f"verify={json.dumps(verify, default=str)}; position may remain on-chain"
            )
            approvals_store.move_to(signal_id, approvals_store.APPROVED_DIR)
        else:
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


def do_sweep_dust_swaps(cfg: Dict[str, Any], rails: Dict[str, Any]) -> None:
    """Execute pending dust swaps now, keeping only the latest record per mint.

    Only usable when mode is auto. Older duplicate records for the same mint
    are marked skipped so they are not retried.
    """
    if cfg.get("mode") != "auto":
        print("Executor is not in auto mode; refusing to sweep dust swaps.")
        sys.exit(1)

    records = dust_queue.list_pending()
    if not records:
        print("No dust swaps pending review.")
        return

    # Keep the latest record per mint to avoid swapping the same token twice.
    by_mint: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        signal = rec.get("signal", {})
        mint = signal.get("mint")
        if not isinstance(mint, str):
            continue
        existing = by_mint.get(mint)
        if existing is None:
            by_mint[mint] = rec
            continue
        existing_signal = existing.get("signal", {})
        try:
            existing_ts = datetime.fromisoformat(str(existing_signal.get("created_at", "1970-01-01T00:00:00+00:00")).replace("Z", "+00:00"))
        except ValueError:
            existing_ts = datetime.min.replace(tzinfo=timezone.utc)
        try:
            rec_ts = datetime.fromisoformat(str(signal.get("created_at", "1970-01-01T00:00:00+00:00")).replace("Z", "+00:00"))
        except ValueError:
            rec_ts = datetime.min.replace(tzinfo=timezone.utc)
        if rec_ts > existing_ts:
            by_mint[mint] = rec

    if not by_mint:
        print("No actionable dust swaps found.")
        return

    executed_ids: List[str] = []
    failed_ids: List[str] = []
    skipped_ids: List[str] = []

    for mint, rec in by_mint.items():
        signal = rec.get("signal", {})
        signal_id = signal.get("signal_id")
        print(f"Sweeping {signal_id} (mint={mint})...")
        status, details = _handle_swap_to_usdc(signal, cfg, rails)
        print(f"  {status}: {details}")
        journal_append(
            signal=signal,
            decision=status,
            details=details,
            rails_hash=rails_hash(RAILS_PATH),
        )
        if status == "executed":
            try:
                trade_ledger.append_row(
                    trade_ledger.build_row(signal, status, details, _now_utc_iso())
                )
            except Exception as exc:
                print(f"[{utc_now()}] ledger row append failed: {exc}")
            dust_queue.update_status(signal_id, "executed")
            executed_ids.append(signal_id)
        else:
            dust_queue.update_status(signal_id, status)
            failed_ids.append(signal_id)

    # Mark older duplicate records as skipped.
    for rec in records:
        signal = rec.get("signal", {})
        signal_id = signal.get("signal_id")
        mint = signal.get("mint")
        if signal_id in executed_ids or signal_id in failed_ids:
            continue
        if by_mint.get(mint) is not None and by_mint.get(mint, {}).get("signal", {}).get("signal_id") != signal_id:
            dust_queue.update_status(signal_id, "skipped")
            skipped_ids.append(signal_id)

    print(f"\nSweep complete: executed={len(executed_ids)}, failed={len(failed_ids)}, skipped duplicates={len(skipped_ids)}")


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
    parser.add_argument("--sweep-dust-swaps", action="store_true", help="Execute pending dust swaps now (auto mode only)")
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
    if args.sweep_dust_swaps:
        do_sweep_dust_swaps(cfg, rails)
        return
    if args.check:
        do_check(cfg)
        return

    if args.once:
        _capital_epilogue(cfg, run_once(cfg, rails))
        return

    while True:
        _capital_epilogue(cfg, run_once(cfg, rails))
        time.sleep(cfg.get("heartbeat_interval_seconds", 60))


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)
