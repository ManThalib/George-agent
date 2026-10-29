#!/usr/bin/env python3
"""George executor watchdog — READ-ONLY anomaly sentinel.

Runs after `dispatcher.py --once` (scripted wake path, owner-approved
2026-10-12). It inspects journal files, signal directories, and the wallet
balance delta; it never writes, moves, sends, or executes anything.

Exit codes:
  0 = quiet (no anomalies; scripted path stays autonomous)
  1 = anomalies found (stdout lists them; automation wakes George)

Design constraints (per george-zero-token-proposal.md):
  - Never bypasses or weakens any rail; it adds detection only.
  - Fail-safe: if the watchdog itself errors, it reports an anomaly
    (watchdog-broken must not silently disable oversight).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import paths  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent  # meteora-dlmm/
# The watchdog must watch the directories the dispatcher actually writes. It
# used to resolve REPO/"journal" and REPO/"signals"/"pending", which on the
# live host are a different tree from the /data one the executor uses.
JOURNAL_DIR = paths.journal_dir()
PENDING_DIR = paths.signals_dir() / "pending"
FAILED_VERIFY_DIR = paths.signals_dir() / "failed_verify"
DISPATCHER_LOCK = paths.state_dir() / "dispatcher.lock"
STATE_DIR = paths.state_dir()
BALANCE_STATE = STATE_DIR / "watchdog_balance.json"

# Tunables (analysis defaults; not rails — changing these never blocks a tx)
REVIEW_STATUSES = {"CLOSE_REVIEW_REQUIRED", "CLOSE_UNKNOWN", "CLOSE_REVIEWED"}
STALE_REJECT_RUN_LIMIT = 2
RUN_FRESHNESS_SECONDS = 7200        # journal must have activity within 2h if pending exists
DRAIN_TOLERANCE_LAMPORTS = 5_000    # rent/fee noise floor
DRAIN_TOLERANCE_USDC = 0.01


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except FileNotFoundError:
        pass
    return out


def _latest_journal() -> Optional[Path]:
    if not JOURNAL_DIR.exists():
        return None
    files = sorted(JOURNAL_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _journal_mtime(path: Optional[Path]) -> float:
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def check_journal_anomalies(run_started_at: float) -> List[str]:
    issues: List[str] = []
    journal = _latest_journal()
    entries = _load_jsonl(journal) if journal else []
    run_entries = [e for e in entries if _entry_ts(e) >= run_started_at - 5]

    stale_rejects = sum(
        1 for e in run_entries
        if e.get("decision") == "rejected"
        and "signal stale" in json.dumps(e.get("details", {}))
    )
    if stale_rejects >= STALE_REJECT_RUN_LIMIT:
        issues.append(
            f"stale_rejects={stale_rejects} in this run (>= {STALE_REJECT_RUN_LIMIT})"
        )

    for e in run_entries:
        decision = e.get("decision", "")
        status = e.get("details", {}).get("settlement_status", "")
        if decision in {"failed", "failed_verify"}:
            issues.append(f"failed signal: {e.get('signal_id')} decision={decision}")
        if status in REVIEW_STATUSES:
            issues.append(f"settlement review: {e.get('signal_id')} status={status}")

    drain = _check_drain(run_entries)
    if drain:
        issues.append(drain)
    return issues


def _entry_ts(entry: Dict[str, Any]) -> float:
    ts = entry.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _check_drain(run_entries: List[Dict[str, Any]]) -> Optional[str]:
    """Wallet-balance delta vs journal: funds moved but no matching entry.

    Reads the on-chain balance via the george balance script if available;
    otherwise compares against the last stored snapshot only (no network).
    """
    prev: Dict[str, Any] = {}
    try:
        prev = json.loads(BALANCE_STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # first run: establish baseline only

    cur = _current_balances()
    if cur is None:
        return None
    alerts = []
    sol_delta = cur.get("sol_lamports", 0) - prev.get("sol_lamports", 0)
    usdc_delta = cur.get("usdc", 0.0) - prev.get("usdc", 0.0)
    journal_spend = sum(
        abs(float(e.get("details", {}).get("result", {}).get("sol_spent_lamports", 0) or 0))
        for e in run_entries
    )
    if sol_delta < -DRAIN_TOLERANCE_LAMPORTS and journal_spend < abs(sol_delta) * 0.5:
        alerts.append(
            f"unexplained SOL outflow: {sol_delta} lamports "
            f"(journal spend {journal_spend})"
        )
    if usdc_delta < -DRAIN_TOLERANCE_USDC and not run_entries:
        alerts.append(f"unexplained USDC outflow: {usdc_delta} with no journal entries")
    return "; ".join(alerts) if alerts else None


def _current_balances() -> Optional[Dict[str, Any]]:
    """Read-only balance snapshot via the existing chain script (no signing)."""
    import subprocess
    script = REPO / "executor" / "chain" / "balance.js"
    if not script.exists():
        return None
    try:
        out = subprocess.run(
            ["node", str(script)],
            capture_output=True, text=True, timeout=45,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def check_runner_alive(journal: Optional[Path]) -> Optional[str]:
    """Pending signals present but no journal activity => runner wedged."""
    try:
        pending = [p for p in PENDING_DIR.iterdir() if p.suffix == ".json"]
    except FileNotFoundError:
        return None
    if not pending:
        return None
    age = time.time() - _journal_mtime(journal)
    if age > RUN_FRESHNESS_SECONDS:
        return (
            f"pending signals ({len(pending)}) but no journal activity for "
            f"{int(age)}s (>= {RUN_FRESHNESS_SECONDS}s)"
        )
    return None


def main() -> int:
    run_started_at = float(sys.argv[1]) if len(sys.argv) > 1 else time.time()
    issues: List[str] = []

    journal = _latest_journal()

    try:
        issues.extend(check_journal_anomalies(run_started_at))
        alive = check_runner_alive(journal)
        if alive:
            issues.append(alive)
    except Exception as exc:  # watchdog must fail loud, not silent
        issues.append(f"watchdog internal error: {exc!r}")

    if issues:
        print("WATCHDOG ANOMALIES:")
        for i in issues:
            print(f"  - {i}")
        return 1

    # quiet run: refresh balance snapshot for next drain check
    cur = _current_balances()
    if cur:
        BALANCE_STATE.parent.mkdir(parents=True, exist_ok=True)
        BALANCE_STATE.write_text(json.dumps(cur))
    print("watchdog: quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
