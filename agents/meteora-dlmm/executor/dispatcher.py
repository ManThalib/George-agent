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
from common import paths
from common.config_loader import ConfigError, load_config
from common.journal import append as journal_append
from common.rails_loader import RAILS_PATH, load_rails
from common.signal_validator import (
    SignalValidationError,
    signal_age_seconds,
    validate_core,
)
from common.chain_client import ChainError, simulate as chain_simulate, send as chain_send
from common import live_preflight
from common import mirror
import common.approvals as approvals_store
import common.dust_queue as dust_queue
import common.exposure_guard as exposure_guard
from common.redact import redact_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ledger"))
import ledger as trade_ledger  # noqa: E402  (executor-local ledger module)


# --------------------------------------------------------------------------
# In-wake rails hash cache (avoid re-hashing SAFETY_RAILS.md per signal)
# --------------------------------------------------------------------------
_rails_hash_cache: Dict[str, str] = {}


def _cached_rails_hash(rails_path: str) -> str:
    """Return cached rails hash, computing once per process lifetime."""
    if rails_path not in _rails_hash_cache:
        _rails_hash_cache[rails_path] = rails_hash(Path(rails_path))
    return _rails_hash_cache[rails_path]


def _resolve_signals_dir() -> Path:
    """Signals root, overridable via GEORGE_SIGNALS_DIR.

    The pipeline driver exports the same directory it hands to Sheldon, so
    producer and consumer cannot drift onto different paths. Accepts either the
    signals root or a `.../pending` path and normalizes to the root.
    """
    raw = os.environ.get("GEORGE_SIGNALS_DIR", "").strip()
    if not raw:
        return paths.signals_dir()
    path = Path(raw)
    return path.parent if path.name == "pending" else path


SIGNALS_DIR = _resolve_signals_dir()
FAILED_VERIFY_DIR = SIGNALS_DIR / "failed_verify"
KILL_FILE = paths.kill_file()

# Exit status for an owner-initiated KILL halt. Distinct from 1 (cycle
# failure) so monitoring can tell "stopped on purpose" from "broke".
EXIT_KILLED = 75

# --- Capital refresh chain (swap -> wallet rescan -> Sheldon re-entry) ---
# After a successful prep swap the wallet changed; Missy's wallet screener
# re-reads balances and Sheldon re-evaluates (emitting the open when funded).
# Repo root for the sibling agents. Overridable so the same executor works in
# the /data layout and in the dev workspace checkout.
_AGENTS_ROOT = str(paths.agents_root())
WALLET_REFRESH_SCRIPT = os.environ.get(
    "WALLET_REFRESH_SCRIPT",
    f"{_AGENTS_ROOT}/missy-agent/wallet_screener/run_wallet.sh",
)
WALLET_SCAN_DIR = paths.wallet_scan_dir()
POSITION_REFRESH_SCRIPT = os.environ.get(
    "POSITION_REFRESH_SCRIPT",
    f"{_AGENTS_ROOT}/missy-agent/position_screener/run_positions.sh",
)
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SHELDON_CYCLE_CMD = [
    "python3",
    os.environ.get("SHELDON_CYCLE_SCRIPT", f"{_AGENTS_ROOT}/Sheldon-agent/run_cycle.py"),
    "--write-signals",
    # Re-entry must read and write the same state the driver used. Before
    # 2026-10-19 it ran with Sheldon's own defaults, so close-settlement
    # state lived in a second file: the driver saw 32/32 settled while the
    # capital-refresh re-entry saw 2 stale CLOSE_EMITTED entries and blocked
    # every open. Live paths are the driver's (GEORGE_STATE_DIR etc.), with
    # the same defaults run_pipeline.sh exports when the env is absent.
    "--pools-dir", os.environ.get(
        "MISSY_POOL_SCAN_DIR", str(paths.pool_scan_dir())),
    "--positions-dir", os.environ.get(
        "MISSY_POSITION_SCAN_DIR", str(paths.position_scan_dir())),
    "--wallet-scans-dir", str(WALLET_SCAN_DIR),
    "--state-dir", os.environ.get("GEORGE_STATE_DIR", str(paths.state_dir())),
    "--memory-dir", os.environ.get("GEORGE_MEMORY_DIR", str(paths.agent_root() / "memory")),
    "--signals-dir", os.environ.get(
        "GEORGE_SIGNALS_DIR", str(SIGNALS_DIR / "pending")),
]
REENTRY_STATE_PATH = paths.state_dir() / "capital_reentry.json"
LOCK_PATH = paths.state_dir() / "dispatcher.lock"
MAX_CAPITAL_REENTRIES_PER_HOUR = 8
# Close bursts are bounded per close (one follow-up each) but a burst of
# several closes in one hour must not starve on the swap cap (owner-approved
# 2026-10-10: separate, higher close-refresh re-entry cap).
MAX_CLOSE_REENTRIES_PER_HOUR = 20
# A prep swap re-enters the dispatcher after Sheldon re-scores (to process
# the open, or another prep swap). Depth bounds the in-process chain; the
# hourly re-entry guard above is the outer backstop.
MAX_CAPITAL_CHAIN_DEPTH = 3

# Prep swaps processed per run_once wake. A deferred-doorbell backlog used to
# let several preps execute at once (seen live 2026-10-05 15:48: a double buy
# plus a sell-back). Only preps are capped; closes and claims are never
# deferred. Override with GEORGE_MAX_PREPS_PER_RUN.
DEFAULT_MAX_PREPS_PER_RUN = 1

# Successful mutations that free capital or change the position set enough
# that the pipeline must re-scan before Sheldon re-decides (plan 03 Change F).
# The refresh fires at the run_once epilogue, outside the dispatcher lock.
CLOSE_REFRESH_ACTIONS = {"close", "rotate", "rebalance"}

# Sheldon prep swap signals are named sheldon-prep-<base>-<idx>.json.
PREP_SIGNAL_PREFIX = "sheldon-prep-"

# Wrapped-SOL mint: a USDC->WSOL prep swap is both the SOL gas top-up and the
# SOL-side position token — the same swap either way, so the priority sorter
# ranks it as a gas top-up (plan 03 Change G rank 3, mint-based, no reason
# parsing). Other preps are target-token buys (rank 4).
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Explicit action priority (plan 03 Change G): de-risk first, then make gas
# available, then prepare position tokens, open only after readiness, dust
# cleanup, claims last. An explicit sort replaces filename order so a
# backlog of stale preps can never delay a close.
#   1 close
#   2 rotate / rebalance / remove_liquidity (mutations that de-risk or close)
#   3 prep swap USDC -> WSOL (gas top-up / SOL side)
#   4 prep swap USDC -> target token
#   5 open / add_liquidity / unknown (unknowns are rejected by validation)
#   6 swap_to_usdc (dust -> USDC)
#   7 claims
_ACTION_PRIORITY = {
    "close": 1,
    "rotate": 2,
    "rebalance": 2,
    "remove_liquidity": 2,
    "open": 5,
    "add_liquidity": 5,
    "swap_to_usdc": 6,
    "claim_fees": 7,
    "claim_rewards": 7,
}
DEFAULT_ACTION_PRIORITY = 5


def _signal_priority(signal_path: Path) -> Tuple[int, str]:
    """(priority rank, name) for one pending signal path.

    A swap whose output is WSOL is the SOL top-up (rank 3); any other swap
    is a target-token buy (rank 4). Sheldon's preps are the only plain-swap
    emitter here, so this is content-based, not filename-based.
    Unreadable or unknown signals take the neutral default rank and are
    left to the normal validation path; sorting must never reject anything.
    """
    name = signal_path.name
    try:
        signal = load_json(signal_path)
    except Exception:
        return (DEFAULT_ACTION_PRIORITY, name)
    action = signal.get("action")
    if action == "swap":
        # Sheldon's preps are the only plain-swap emitter in this pipeline;
        # rank by output mint, not filename, so the class matches the plan.
        return (3 if signal.get("output_mint") == WSOL_MINT else 4, name)
    return (_ACTION_PRIORITY.get(action, DEFAULT_ACTION_PRIORITY), name)


def _iter_pending_by_priority() -> List[Path]:
    """Pending signals in explicit priority order (plan 03 Change G).

    Stable on filename within a rank, so ordering is deterministic for a
    given set of files. A close always outranks swaps and opens; KILL and
    the per-wake prep cap are unchanged and applied after this sort.
    """
    pending = _iter_pending()
    pending.sort(key=_signal_priority)
    return pending


def _max_preps_per_run() -> int:
    try:
        return max(0, int(os.environ.get("GEORGE_MAX_PREPS_PER_RUN",
                                         str(DEFAULT_MAX_PREPS_PER_RUN))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PREPS_PER_RUN


def _is_prep_signal_path(signal_path: Path) -> bool:
    return signal_path.name.startswith(PREP_SIGNAL_PREFIX)


def _prep_is_fresh(signal_path: Path, cfg: Dict[str, Any]) -> bool:
    """True when a prep signal is still inside the freshness window.

    A leftover prep that has already aged out will be rejected by validation
    no matter what, so it must not consume a per-wake prep slot: doing so let
    a stale backlog starve the fresh prep that was actually actionable (the
    deferred file sorts first by name, so the fresh prep lost the slot on
    every subsequent wake).

    Unusable or unreadable signals report fresh and are left to the normal
    validation path, so the cap is never silently bypassed.
    """
    try:
        signal = load_json(signal_path)
    except Exception:
        return True
    age = signal_age_seconds(signal)
    if age is None:
        return True
    return age <= float(cfg.get("signal_max_age_seconds", 300))


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
    """Reconcile tracked exposure entries against the live position scan.

    Runs once per run_once. Evidence-based (see
    exposure_guard.reconcile_positions): fresh + history-complete scans
    add genuinely-new positions, refresh values, drop dead entries, and
    update rebalance replacements in place. A stale scan is skipped.
    Never raises: reconciliation is a safety net, not a gate.
    """
    try:
        positions, mtime = exposure_guard.load_scan_positions()
        complete = exposure_guard.scan_history_complete()
        report = exposure_guard.reconcile_positions(
            positions, scan_mtime=mtime, history_complete=complete)
        if report.get("skipped"):
            print(f"[{utc_now()}] exposure reconcile skipped: {report['skipped']}")
            return
        for entry in report.get("added") or []:
            print(f"[{utc_now()}] exposure reconcile added "
                  f"{entry.get('pool_address')} (wallet {entry.get('wallet_id')}, "
                  f"${entry.get('position_usd')})")
        for entry in report.get("updated") or []:
            print(f"[{utc_now()}] exposure reconcile value update "
                  f"{entry.get('pool_address')} "
                  f"${entry.get('position_usd')} -> ${entry.get('new_position_usd')}")
        for entry in report.get("removed") or []:
            print(f"[{utc_now()}] exposure reconcile removed dead entry "
                  f"{entry.get('pool_address')} pos={entry.get('position_id')} "
                  f"(${entry.get('position_usd')}): {entry.get('reason')}")
        for entry in report.get("unmatched") or []:
            print(f"[{utc_now()}] exposure reconcile: tracked position "
                  f"{entry.get('pool_address')} absent from scan (kept; needs a "
                  f"confirmed close to remove)")
    except Exception as exc:
        print(f"[{utc_now()}] exposure reconcile failed: {exc}")


def _reentry_window(state: Dict[str, Any], now: float,
                    key: str = "timestamps") -> List[float]:
    """Timestamps of capital re-entries inside the last hour."""
    stamps = state.get(key) or []
    return [t for t in stamps if isinstance(t, (int, float)) and now - t < 3600]


def _reentry_allowed(state: Dict[str, Any], now: float,
                     cap: int = MAX_CAPITAL_REENTRIES_PER_HOUR,
                     key: str = "timestamps") -> Tuple[bool, str]:
    recent = _reentry_window(state, now, key)
    if len(recent) >= cap:
        return False, f"loop guard: {len(recent)} re-entries in the last hour (cap {cap})"
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


def _is_usdc_scan_zero_but_has_assets(scan_path: str) -> bool:
    """Return True if the USDC amount in the scan is zero but there are any
    other assets with non-zero value."""
    try:
        with open(scan_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return False
    usdc_amount = 0
    has_other_assets = False
    for a in data.get("assets", []):
        if a.get("mint") == USDC_MINT:
            try:
                usdc_amount = int(a.get("amount_raw") or 0)
            except (TypeError, ValueError):
                usdc_amount = 0
        else:
            try:
                amount = int(a.get("amount_raw") or 0)
                if amount > 0:
                    has_other_assets = True
            except (TypeError, ValueError):
                pass
    return usdc_amount == 0 and has_other_assets


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
        if _is_usdc_scan_zero_but_has_assets(cur_path):
            # USDC amount is zero but there are other assets; treat as invalid scan and retry.
            continue
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


def _absence_note(target: Dict[str, Any],
                  positions: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """(absent_or_not_blocking, human note) for one closed position.

    A `close` asserts the position is really gone from the fresh scan
    (strict). A rotate/rebalance may legitimately re-open in the same pool,
    so a still-present pool is reported as not asserted, never as failure.
    Mirror-wallet targets are never asserted: run_positions.sh scans MAIN.
    """
    wallet_id = str(target.get("wallet_id") or "main")
    pool = str(target.get("pool_address") or "")
    if wallet_id != "main":
        return True, f"not asserted (wallet {wallet_id} is not scanned by the position screener)"
    # Closed/zero-liquidity rows are not live positions (bug #4): a scan that
    # still lists a just-closed position as a closed row must not fail the
    # absence assertion.
    same_pool = [p for p in positions
                 if exposure_guard._scan_key(p) == (wallet_id, pool)
                 and exposure_guard.row_is_open(p)]
    position_address = str(target.get("position_address") or "")
    if not same_pool:
        return True, "position absent from fresh scan"
    if position_address and not any(
            any(str(p.get(k) or "") == position_address
                for k in ("position_address", "position_id", "position_mint"))
            for p in same_pool):
        return True, "old position gone; pool reused by a new position"
    if target.get("strict"):
        return False, (f"closed position still present in fresh scan "
                       f"({len(same_pool)} position(s) in {pool[:8]}...)")
    return True, "not asserted: target pool still has positions (reopen expected)"


def _run_position_refresh(
        cfg: Dict[str, Any],
        absence_targets: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Run Missy's position scan and check the closed positions are gone.

    Returns (result string, per-target absence results). A scan that fails
    to refresh reports FAILED and leaves absence unknown (never asserted).
    Read-only for the wallet: the screener only reads chain state.
    """
    rpc = cfg.get("rpc_https_url")
    wallet = cfg.get("wallet_public_key")
    if not rpc or not wallet:
        return "skipped (missing rpc_https_url / wallet_public_key in config)", []
    env = {**os.environ, "SOLANA_RPC_URL": rpc, "SOLANA_PUBLIC_WALLET": wallet}
    _, pre_mtime = exposure_guard.load_scan_positions()
    last_out = ""
    for attempt in range(4):
        try:
            proc = subprocess.run(
                ["bash", POSITION_REFRESH_SCRIPT], env=env, capture_output=True,
                text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "FAILED (timeout after 120s)", []
        except Exception as exc:
            return f"FAILED ({exc})", []
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
            return f"FAILED exit={proc.returncode}: {tail[0][:160]}", []
        last_out = (proc.stdout or "").strip().splitlines()[-1:]
        positions, cur_mtime = exposure_guard.load_scan_positions()
        if cur_mtime <= pre_mtime:
            if attempt < 3:
                time.sleep(6)  # let the refresh land a new scan
                continue
            return (f"ok (scan not refreshed: mtime unchanged) "
                    f"-> {last_out[0] if last_out else ''}"), []
        absence = [dict(target, absent=absent, note=note)
                   for target in absence_targets
                   for absent, note in [_absence_note(target, positions)]]
        return f"ok -> {last_out[0] if last_out else ''}", absence
    return "FAILED (unreachable)", []


def _run_sheldon_reentry() -> Tuple[str, str, str]:
    """Run Sheldon's cycle and return (status, stdout, stderr).

    A leftover prep that has already aged out will be rejected by validation
    no matter what, so it must not consume a per-wake prep slot: doing so let
    a stale backlog starve the fresh prep that was actually actionable (the
    deferred file sorts first by name, so the fresh prep lost the slot on
    every subsequent wake).

    Unusable or unreadable signals report fresh and are left to the normal
    validation path, so the cap is never silently bypassed.
    """
    try:
        proc = subprocess.run(
            SHELDON_CYCLE_CMD, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return "FAILED (timeout after 300s)", "", ""
    except Exception as exc:
        return f"FAILED ({exc})", "", ""
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    tail = stdout.strip().splitlines()[-1:] or ["no output"]
    if proc.returncode != 0:
        err_tail = stderr.strip().splitlines()[-1:] or [""]
        return f"FAILED exit={proc.returncode}: {err_tail[0][:160]}", stdout, stderr
    return f"ok: {tail[0][:160]}", stdout, stderr


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
    sheldon_result, sheldon_stdout, sheldon_stderr = _run_sheldon_reentry()
    print(f"[{utc_now()}] Sheldon re-entry: {sheldon_result}")

    followup_result = "skipped (no new signals)"
    if "WAKE_GEORGE:" in sheldon_stdout or "WAKE_GEORGE:" in sheldon_stderr:
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
            if open_like:
                # Owner decision 2026-10-10 (#16): mirror open legs get the
                # same live SOL/token preflight as MAIN, against the MIRROR
                # wallet's balances. Fail-closed: an unverifiable mirror
                # balance defers the leg (open legs are already gated on the
                # next cycle; closes never run through this branch).
                mirror_cfg = {**cfg,
                              "wallet_public_key": leg_req["wallet_public_key"],
                              "wallet_registry": {}}
                reject = _open_preflight(leg_signal, mirror_cfg, rails)
                if reject is not None:
                    mirror.record_leg(signal.get("signal_id", ""), wallet_id,
                                      leg_signal["action"], "preflight_blocked",
                                      json.dumps(reject, default=str)[:200])
                    mirror.record_drift(signal.get("signal_id", ""), wallet_id,
                                        leg_signal["action"],
                                        "mirror preflight failed; deferred")
                    legs.append({"wallet_id": wallet_id, "status": "preflight_blocked",
                                 "detail": str(reject.get("error_code") or reject)[:200]})
                    continue
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


def _swap_error_class(err_text: str) -> str:
    """Classify a swap chain error for the bounded retry policy.

    On-chain custom program errors (SPL/DEX 'Custom': NNNN, e.g. Whirlpool
    6024 InvalidTokenMintOrder) are DETERMINISTIC: the built route/tx is
    wrong; retrying it burns fees. Transient classes (blockhash, timeout,
    RPC/network) may legitimately succeed on a second attempt. slippage is
    its own class: a fresh quote at a worse price will not fix it either.
    """
    text = str(err_text or "")
    if "Custom" in text and any(c.isdigit() for c in text):
        return "deterministic"
    for token in ("blockhash", "BlockhashNotFound", "timeout", "timed out",
                  "ECONNRESET", "ETIMEDOUT", "fetch failed", "429",
                  "503", "502", "Connection error"):
        if token in text:
            return "transient"
    if "slippage" in text.lower() or "SlippageTolerance" in text:
        return "slippage"
    return "unknown"


def _handle_swap_to_usdc(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Auto-execute a dust swap to USDC when the executor is in auto mode.

    Bounded retry policy (2026-10-17, SPL Custom 6024 window): at most ONE
    additional attempt, only for transient-class errors, >=60s backoff, and
    never the same serialized transaction (each attempt re-quotes fresh).
    Deterministic (on-chain program) and slippage errors fail immediately —
    re-sending the same route cannot succeed. The signal is left un-moved
    on failure so the normal queue rails (age caps, dust re-queue) own the
    next attempt on a future cycle.
    """
    max_attempts = 2
    attempts = []
    for attempt in range(max_attempts):
        if attempt:
            time.sleep(60)
        try:
            req = _build_dust_swap_request(signal, cfg, rails)
        except (KeyError, TypeError, ValueError) as exc:
            return "rejected", {"stage": "dust_swap_build", "error": str(exc)}

        try:
            sim = chain_simulate(req)
        except ChainError as exc:
            attempts.append({"stage": "simulate", "error": str(exc)[:300]})
            klass = _swap_error_class(str(exc))
            if klass != "transient" or attempt == max_attempts - 1:
                return "failed", {"stage": "simulate", "error": str(exc),
                                   "error_class": klass, "attempts": attempts}
            continue
        except Exception as exc:
            return "failed", {"stage": "simulate", "error": str(exc),
                               "traceback": traceback.format_exc()}

        if not _sim_ok(sim):
            return "rejected", {"stage": "simulate", "error": "simulation failed",
                                "simulation": sim.get("simulation")}

        try:
            result = chain_send(req)
        except ChainError as exc:
            attempts.append({"stage": "send", "error": str(exc)[:300]})
            klass = _swap_error_class(str(exc))
            if klass != "transient" or attempt == max_attempts - 1:
                return "failed", {"stage": "send", "error": str(exc),
                                   "error_class": klass, "attempts": attempts}
            continue
        except Exception as exc:
            return "failed", {"stage": "send", "error": str(exc),
                               "traceback": traceback.format_exc()}

        details: Dict[str, Any] = {"result": result}
        if attempt:
            details["retry_attempt"] = attempt + 1
        for key in ("confirmed_via", "fallback_broadcast", "confirmations"):
            if isinstance(result, dict) and result.get(key) is not None:
                details[key] = result[key]
        return "executed", details
    return "failed", {"stage": "exhausted", "attempts": attempts}


def process_signal(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    action = signal.get("action")

    # Per-signal cache boundary: a mid-wake swap changes real balances, so
    # every signal must see fresh live state in its preflight (not whatever
    # an earlier leg of this wake cached).
    live_preflight.clear_preflight_caches()

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

    # Live balance preflight (Change B-D): re-check the wallet's real SOL and
    # token balances immediately before simulation. The signal may come from a
    # stale scan, an unsettled prep swap, or a duplicate pending open; a
    # shortfall is reported mechanically here instead of surfacing as a raw
    # Solana "insufficient funds" error from a later simulation/send.
    if action in {"open", "rotate", "rebalance"} or (action == "add_liquidity"):
        reject = _open_preflight(signal, cfg, rails)
        if reject is not None:
            return "rejected", reject

    mode = cfg.get("mode", "dry_run")
    if mode == "auto":
        return _handle_auto(signal, cfg, rails)
    if mode == "confirm_each":
        return _handle_confirm_each(signal, cfg, rails)

    decision, details = _run_simulation(signal, cfg, rails)
    return decision, details


def _open_preflight(signal: Dict[str, Any], cfg: Dict[str, Any],
                    rails: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Live SOL/token preflight for an open.

    Returns a journal-ready rejection dict when the open must not proceed,
    or None when the live balances satisfy the DEX/signal/position-size-
    aware requirement. Non-open actions are always allowed through.
    """
    if signal.get("action") not in {"open", "add_liquidity", "rotate", "rebalance"}:
        return None
    if signal.get("action") in {"rotate", "rebalance"} and not signal.get("liquidity"):
        # Close-only leg: never gate a de-risking mutation (owner decision
        # 2026-10-10 #15: preflight applies to the OPEN leg only).
        return None
    try:
        ok, details = live_preflight.preflight_open(signal, cfg, rails)
    except Exception as exc:  # fail closed: never open on an unverifiable state
        return {
            "stage": "preflight_sol",
            "error_code": "PREFLIGHT_UNAVAILABLE",
            "error": str(exc),
        }
    return None if ok else details


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


def _trigger_close_refresh(cfg: Dict[str, Any], closes: List[Dict[str, Any]]) -> bool:
    """After successful close(s)/rotate/rebalance: refresh Missy's position
    scan (the authoritative view of what still exists) and wallet scan, then
    let Sheldon re-decide on real state and wake George only if it wrote new
    signals (plan 03 Change F).

    Bounds: one follow-up wake per refresh, the shared hourly re-entry cap,
    MAX_CAPITAL_CHAIN_DEPTH for in-process chains, one dispatcher lock (the
    epilogue runs after run_once releases it). A strict close whose position
    is STILL in the fresh scan does not re-enter Sheldon — re-deciding on a
    position that may still exist is exactly the close -> scan -> close loop
    Change F forbids; the owner is notified instead.

    Failures here never undo the close; they only cost latency (the hourly
    pipeline re-runs Sheldon anyway).
    """
    ids = ",".join(str(c.get("signal_id")) for c in closes)
    if os.environ.get("GEORGE_SKIP_CAPITAL_REFRESH"):
        print(f"[{utc_now()}] close refresh skipped (GEORGE_SKIP_CAPITAL_REFRESH set)")
        return True
    if KILL_FILE.exists():
        print(f"[{utc_now()}] close refresh skipped (KILL file present)")
        return True
    depth = int(os.environ.get("GEORGE_CAPITAL_DEPTH") or 0)
    if depth >= MAX_CAPITAL_CHAIN_DEPTH:
        _notify_owner(
            f"CLOSE CHAIN DEPTH LIMIT signal={ids}: depth {depth} >= "
            f"{MAX_CAPITAL_CHAIN_DEPTH}; remaining legs left to the next cycle"
        )
        return

    now = time.time()
    state = _load_reentry_state()
    allowed, reason = _reentry_allowed(state, now,
                                       cap=MAX_CLOSE_REENTRIES_PER_HOUR,
                                       key="close_timestamps")
    if not allowed:
        _notify_owner(f"CLOSE REFRESH SKIPPED signal={ids}: {reason}")
        return True
    state["close_timestamps"] = _reentry_window(state, now, "close_timestamps") + [now]
    _save_reentry_state(state)

    targets = [{
        "wallet_id": c.get("wallet_id") or "main",
        "pool_address": c.get("pool_address") or "",
        "position_address": c.get("position_id") or c.get("position_address") or "",
        "strict": c.get("action") == "close",
    } for c in closes]

    print(f"[{utc_now()}] close refresh for {ids} (depth {depth}): position rescan...")
    position_result, absence = _run_position_refresh(cfg, targets)
    print(f"[{utc_now()}] position rescan: {position_result}")

    # A strict close that is not confirmed gone blocks re-entry — and so does
    # an absence we could not check at all (scan failed / never refreshed):
    # re-deciding on unverified state is exactly the close->scan->close loop
    # Change F forbids. Scans still run so the owner sees the wallet state.
    blocked = (len(absence) != len(targets)
               or any(not a.get("absent") for a in absence))
    print(f"[{utc_now()}] close refresh: wallet rescan...")
    wallet_result = _run_wallet_refresh(cfg, 0, ambiguous=True)
    print(f"[{utc_now()}] wallet rescan: {wallet_result}")

    sheldon_result = "skipped (closed position still in fresh position scan)" if blocked else "not run"
    followup_result = "skipped"
    if not blocked:
        print(f"[{utc_now()}] close refresh: Sheldon re-entry...")
        sheldon_result, sheldon_stdout, sheldon_stderr = _run_sheldon_reentry()
        print(f"[{utc_now()}] Sheldon re-entry: {sheldon_result}")
        if "WAKE_GEORGE:" in sheldon_stdout or "WAKE_GEORGE:" in sheldon_stderr:
            print(f"[{utc_now()}] close refresh: dispatcher follow-up...")
            followup_result = _run_dispatcher_followup(depth)
            print(f"[{utc_now()}] dispatcher follow-up: {followup_result}")

    for close, target, ab in zip(closes, targets, absence or [{} for _ in closes]):
        try:
            journal_append(
                {"signal_id": close.get("signal_id"), "action": close.get("action"),
                 "wallet_id": target.get("wallet_id"), "pool_address": target.get("pool_address")},
                "close_refresh",
                {"position_scan": position_result,
                 "absence": {"asserted": bool(target.get("strict")),
                             "absent": ab.get("absent"), "note": ab.get("note")},
                  "wallet_scan": wallet_result,
                  "sheldon": sheldon_result,
                  "followup": followup_result,
                  "depth": depth},
                _cached_rails_hash(str(RAILS_PATH)),
            )
        except Exception as exc:
            print(f"[{utc_now()}] close refresh journal failed: {exc}")

    if blocked:
        _notify_owner(
            f"CLOSE REFRESH INCOMPLETE signal={ids}: position scan "
            f"{position_result}" + (
                f"; {'; '.join(a.get('note', '') for a in absence if not a.get('absent'))}"
                if absence else "; absence unverified (no fresh scan)"
            ) + ". Sheldon re-entry withheld to avoid a close->scan->close "
            "loop; inspect the fresh position scan before acting."
        )
        return True
    else:
        _notify_owner(
            f"CLOSE REFRESH signal={ids}: position scan {position_result}; "
            f"wallet scan {wallet_result}; Sheldon {sheldon_result}; "
            f"follow-up {followup_result}"
        )
        return False


def _capital_epilogue(cfg: Dict[str, Any], executed: List[Dict[str, Any]]) -> bool:
    """After run_once releases the dispatcher lock: mirror-drift report, then
    the refresh chains (wallet rescan -> Sheldon re-entry -> bounded
    dispatcher follow-up) for the mutations that need them. Returns True
    when a close-refresh ended incomplete (owner decision 2026-10-10 #20:
    the --once run exits non-zero so automation/monitoring sees it)."""
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
    if swaps:
        expected, ambiguous = _expected_usdc_delta(swaps)
        ids = ",".join(str(s.get("signal_id")) for s in swaps)
        _trigger_capital_refresh(cfg, ids, expected, ambiguous)

    closes = [s for s in executed if s.get("action") in CLOSE_REFRESH_ACTIONS]
    if closes:
        return _trigger_close_refresh(cfg, closes)
    return False


def _prune_failed_verify(max_age_days: float = 30.0) -> int:
    """Cap the failed_verify pile: delete files older than max_age_days.

    Live 2026-10-17: 33 files (mostly verifier false alarms, since fixed)
    accumulated with no retention. Every file there was journaled and
    owner-notified at move time; pruning old copies does not lose the
    audit trail (journal + ledger keep it). Returns the count removed.
    """
    try:
        if not FAILED_VERIFY_DIR.exists():
            return 0
        cutoff = time.time() - max_age_days * 86400.0
        removed = 0
        for f in FAILED_VERIFY_DIR.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            print(f"[{utc_now()}] failed_verify pruned {removed} file(s) "
                  f"older than {max_age_days:.0f}d")
        return removed
    except Exception as exc:
        print(f"[{utc_now()}] failed_verify prune failed: {exc}")
        return 0


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
        # before evaluating any new signal (evidence-based; stale scan is
        # skipped).
        _reconcile_exposure()
        _prune_failed_verify()

        pending = _iter_pending_by_priority()
        if not pending:
            print(f"[{utc_now()}] No pending signals.")
            return executed

        print(f"[{utc_now()}] Processing {len(pending)} signal(s)...")
        max_preps = _max_preps_per_run()
        preps_done = 0
        deferred_preps: List[str] = []
        for signal_path in pending:
            # Bound prep swaps per wake: a doorbell backlog must not fire
            # several preps at once. Closes/claims are never deferred. A prep
            # that is already stale never takes a slot — it is rejected by
            # validation and moved out of pending either way, and letting it
            # count would starve the fresh prep behind it.
            if _is_prep_signal_path(signal_path):
                if _prep_is_fresh(signal_path, cfg):
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
                # Bug #9 (live 2026-10-10): an open that failed self-verify
                # must not occupy a tracked slot at full position_usd — the
                # deposit is unconfirmed. The reconcile pass re-adds it from
                # a later scan if it really exists.
                _verify = _self_verify_block(details)
                _unverified = (
                    processed_signal.get("action") in {"open", "add_liquidity"}
                    and (_verify is None or not _verify.get("ok"))
                )
                if _unverified:
                    print(f"[{utc_now()}] exposure tracking skipped for "
                          f"{processed_signal.get('signal_id')}: self-verify "
                          f"failed; reconcile will re-add if the deposit "
                          f"landed")
                else:
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

    # Bug #7b (live 2026-10-10): an executed open whose on-chain self-verify
    # failed (e.g. a zero deposit) must not journal as a plain "executed" —
    # downstream consumers read that as "capital deployed and verified".
    journal_status = status
    if status == "executed" and signal.get("action") in {"open", "add_liquidity"}:
        verify = _self_verify_block(details)
        if verify is None or not verify.get("ok"):
            journal_status = "executed_unverified"

    journal_append(
        signal=signal,
        decision=journal_status,
        details=details,
        rails_hash=_cached_rails_hash(str(RAILS_PATH)),
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
        sys.exit(EXIT_KILLED)

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

    # Live balance preflight also guards the (re-)send path: a signal that
    # passed validation when queued may no longer be funded when approved.
    reject = _open_preflight(signal, cfg, rails)
    if reject is not None:
        journal_append(
            signal=signal,
            decision="rejected",
            details=reject,
            rails_hash=_cached_rails_hash(str(RAILS_PATH)),
        )
        approvals_store.move_to(signal_id, approvals_store.REJECTED_DIR)
        print(f"Approval rejected: live preflight failed: {json.dumps(reject, default=str)}")
        return

    decision, details = _do_send(signal, cfg, rails)
    journal_append(
        signal=signal,
        decision=decision,
        details=details,
        rails_hash=_cached_rails_hash(str(RAILS_PATH)),
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
        rails_hash=_cached_rails_hash(str(RAILS_PATH)),
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
            rails_hash=_cached_rails_hash(str(RAILS_PATH)),
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
    # Never print the endpoint raw: it usually carries the API key in its
    # query string, and truncation preserves a recoverable prefix of it.
    print(f"  rpc: {redact_url(cfg.get('rpc_https_url', ''))}")
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
        incomplete = _capital_epilogue(cfg, run_once(cfg, rails))
        if KILL_FILE.exists():
            # A killed cycle must be distinguishable from a healthy one.
            # Previously this exited 0 and printed "cycle complete", so any
            # monitor (systemd OnFailure, alerting, a human reading the tail)
            # saw a normal run. Distinct status, distinct marker line.
            print(f"[{utc_now()}] KILLED: halted by KILL file; no signals "
                  f"processed (exit {EXIT_KILLED}).")
            sys.exit(EXIT_KILLED)
        if incomplete:
            # Owner decision 2026-10-10 (#20): an incomplete close-refresh is
            # a failed cycle for automation/monitoring purposes.
            sys.exit(1)
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
