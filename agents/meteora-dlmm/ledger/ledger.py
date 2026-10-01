"""Trade ledger for the multi-DEX executor.

One row per executed on-chain decision. The journal records decisions; the
processed signals carry amounts; enrichment (USD prices, gas, rent) is
resolved at reconcile time and each value carries its source.

Data sources:
    journal/<date>.jsonl            decisions + tx signatures + position_id
    signals/processed/<id>.json     full signal: amounts, tokens, bin_range
    signals/failed_verify/<id>.json same, for opens that failed self-verify
    approvals/failed_verify/*.json  same, for approved-but-unverified opens
    Jupiter price API               token USD prices at reconcile time
    Solana RPC                      tx fee (gas), account lamports (rent)

Usage:
    python3 ledger.py backfill --days 7
    python3 ledger.py reconcile --date 2026-09-30
    python3 report.py --days 7
"""

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENT_ROOT = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm")
JOURNAL_DIR = AGENT_ROOT / "journal"
SIGNALS_DIR = AGENT_ROOT / "signals"
APPROVALS_DIR = AGENT_ROOT / "approvals"
LEDGER_PATH = AGENT_ROOT / "ledger" / "ledger.jsonl"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

SIGNAL_DIRS = [
    SIGNALS_DIR / "processed",
    SIGNALS_DIR / "failed_verify",
    APPROVALS_DIR / "failed_verify",
]


# --------------------------------------------------------------------------
# Ledger row I/O
# --------------------------------------------------------------------------
def append_row(row: Dict[str, Any]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str))
        fh.write("\n")


def load_rows() -> List[Dict[str, Any]]:
    if not LEDGER_PATH.exists():
        return []
    rows = []
    with open(LEDGER_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def rows_for_date(rows: List[Dict[str, Any]], day: str) -> List[Dict[str, Any]]:
    """Rows whose UTC calendar day matches `day` (YYYY-MM-DD)."""
    return [r for r in rows if str(r.get("timestamp_utc", ""))[:10] == day]


# --------------------------------------------------------------------------
# Signal lookup: executed decisions need the amounts from the signal file
# --------------------------------------------------------------------------
def find_signal_file(signal_id: str) -> Optional[Path]:
    for d in SIGNAL_DIRS:
        path = d / f"{signal_id}.json"
        if path.exists():
            return path
    return None


# --------------------------------------------------------------------------
# Enrichment: prices, gas, rent — each recorded with its source
# --------------------------------------------------------------------------
_JUPITER_URL = "https://lite-api.jup.ag/price/v3?ids={ids}"
_PRICE_TTL_SECONDS = 300
_price_cache: Dict[str, Any] = {"at": 0.0, "prices": {}}


def _fetch_prices(mints: List[str]) -> Dict[str, Optional[float]]:
    now = time.time()
    if now - _price_cache["at"] < _PRICE_TTL_SECONDS:
        hit = {m: _price_cache["prices"].get(m) for m in mints if m in _price_cache["prices"]}
        if all(m in hit for m in mints):
            return hit
    out: Dict[str, Optional[float]] = {m: None for m in mints}
    missing = [m for m in mints if m not in _price_cache["prices"] or now - _price_cache["at"] >= _PRICE_TTL_SECONDS]
    url = _JUPITER_URL.format(ids=",".join(missing))
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        for mint in missing:
            entry = data.get(mint) or {}
            try:
                out[mint] = float(entry.get("usdPrice"))
            except (TypeError, ValueError):
                out[mint] = None
        _price_cache["at"] = now
        _price_cache["prices"].update(out)
    except Exception:
        pass
    return out


def token_prices(mints: List[str]) -> Dict[str, Dict[str, Any]]:
    """{mint: {price_usd, source}} — source is jupiter or unavailable."""
    prices = _fetch_prices(list(dict.fromkeys(mints)))
    return {
        mint: {
            "price_usd": prices.get(mint),
            "source": "jupiter" if prices.get(mint) is not None else "unavailable",
        }
        for mint in mints
    }


def _rpc_url() -> Optional[str]:
    for name in ("SOLANA_RPC_URL",):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    try:
        cfg = json.loads((AGENT_ROOT / "config" / "agent.config.json").read_text())
        url = (cfg.get("rpc") or {}).get("https_url", "").strip()
        return url or None
    except Exception:
        return None


def _rpc(method: str, params: list, timeout: int = 15) -> Optional[dict]:
    url = _rpc_url()
    if not url:
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data.get("result")
    except Exception:
        return None


def tx_fee_lamports(signature: str) -> Dict[str, Any]:
    """Solana tx fee in lamports (fixed 5000 unless priority fees add more)."""
    result = _rpc("getTransaction", [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
    if not result:
        return {"fee_lamports": None, "source": "unavailable"}
    meta = result.get("meta") or {}
    fee = meta.get("fee")
    return {
        "fee_lamports": int(fee) if fee is not None else None,
        "source": "rpc_getTransaction",
    }


def account_rent_lamports(address: str) -> Dict[str, Any]:
    """Rent lamports held by an account (position PDAs reclaim this on close)."""
    result = _rpc("getAccountInfo", [address, {"encoding": "base64"}])
    if not result or not result.get("value"):
        return {"rent_lamports": None, "source": "unavailable"}
    lamports = result["value"].get("lamports")
    return {
        "rent_lamports": int(lamports) if lamports is not None else None,
        "source": "rpc_getAccountInfo",
    }


# --------------------------------------------------------------------------
# Signal -> ledger row
# --------------------------------------------------------------------------
def _first_signature(details: Dict[str, Any]) -> Optional[str]:
    result = details.get("result") or {}
    if not isinstance(result, dict):
        return None
    if result.get("signature"):
        return result["signature"]
    for step in result.get("signatures") or []:
        if isinstance(step, dict) and step.get("signature"):
            return step["signature"]
    return None


def _close_signature(details: Dict[str, Any]) -> Optional[str]:
    result = details.get("result") or {}
    if isinstance(result, dict):
        return result.get("close_signature")
    return None


def build_row(signal: Dict[str, Any], decision: str, details: Dict[str, Any],
              timestamp_utc: str) -> Dict[str, Any]:
    """One ledger row per executed signal, amounts and sourcing included."""
    action = signal.get("action")
    row: Dict[str, Any] = {
        "timestamp_utc": timestamp_utc,
        "signal_id": signal.get("signal_id"),
        "action": action,
        "dex": signal.get("dex"),
        "pool_address": signal.get("pool_address"),
        "position_id": signal.get("position_id"),
        "position_usd": signal.get("position_usd"),
        "score": signal.get("score"),
        "bin_range": signal.get("bin_range"),
        "bps": signal.get("bps"),
        "verify_ok": (details.get("result") or {}).get("verify", {}).get("ok")
        if isinstance(details.get("result"), dict) else None,
        "amounts": {},
        "price_source": {},
        "gas": {},
        "rent_recovered": {},
    }

    liq = signal.get("liquidity") or {}
    mints = _signal_mints(signal)
    prices = token_prices(list(mints.values()))
    row["amounts"] = {
        side: {"amount_raw": str(liq.get(side_key, "0")), "mint": mints[side]}
        for side, side_key in (("x", "amount_x"), ("y", "amount_y"))
        if mints.get(side)
    }
    row["price_source"] = {m: prices[m]["source"] for m in mints.values()}

    sig = _first_signature(details)
    if sig:
        row["signature"] = sig
        row["gas"] = tx_fee_lamports(sig)
    close_sig = _close_signature(details)
    if action == "close" and close_sig and close_sig != sig:
        row["close_signature"] = close_sig
        row["gas_close"] = tx_fee_lamports(close_sig)
    if action == "close" and signal.get("position_id"):
        row["rent_recovered"] = account_rent_lamports(signal["position_id"])
    return row


def _signal_mints(signal: Dict[str, Any]) -> Dict[str, str]:
    """Token mints for a signal. Meteora/CLMM convention: x = base, y = quote.

    Derived from the pool name where available (e.g. SOL-USDC); USDC and SOL
    mints are well-known, other tokens stay unsourced rather than guessed.
    """
    known = {USDC_MINT: USDC_MINT, SOL_MINT: SOL_MINT}
    name = str(signal.get("pool") or "")
    mints: Dict[str, str] = {}
    if name and "-" in name:
        x_sym, _, y_sym = name.partition("-")
        for side, sym in (("x", x_sym), ("y", y_sym)):
            sym = sym.strip().upper()
            if sym in ("USDC", "USDC.E", "USDCE"):
                mints[side] = USDC_MINT
            elif sym in ("SOL", "WSOL"):
                mints[side] = SOL_MINT
    return mints


# --------------------------------------------------------------------------
# Journal-driven reconciliation
# --------------------------------------------------------------------------
def _iter_journal_entries(days: int, end: date) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for offset in range(days):
        day = end - timedelta(days=offset)
        path = JOURNAL_DIR / f"{day.isoformat()}.jsonl"
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def _journal_ts_utc(entry: Dict[str, Any]) -> str:
    """Journal timestamps are Asia/Shanghai (+08:00); normalize to UTC."""
    raw = str(entry.get("timestamp") or "")
    try:
        dt = datetime.fromisoformat(raw)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return raw


def reconcile_journal(days: int, end: Optional[date] = None) -> Dict[str, Any]:
    """Rebuild ledger rows from the journal + processed signals.

    Idempotent: rows whose signal_id+decision already exist are skipped.
    """
    end = end or date.today()
    known = {(r.get("signal_id"), r.get("decision")) for r in load_rows()}
    added, skipped, missing_signal = 0, 0, 0
    for entry in _iter_journal_entries(days, end):
        if entry.get("decision") != "executed":
            continue
        signal_id = entry.get("signal_id")
        if (signal_id, "executed") in known:
            skipped += 1
            continue
        path = find_signal_file(signal_id)
        signal: Dict[str, Any] = {}
        if path:
            try:
                signal = json.loads(path.read_text())
            except Exception:
                signal = {}
        else:
            missing_signal += 1
            signal = {
                "signal_id": signal_id,
                "action": entry.get("action"),
                "dex": entry.get("dex"),
                "pool_address": entry.get("pool_address"),
                "position_id": (entry.get("details") or {}).get("result", {}).get("position_id")
                if isinstance((entry.get("details") or {}).get("result"), dict) else None,
            }
        row = build_row(signal, "executed", entry.get("details") or {},
                        _journal_ts_utc(entry))
        append_row(row)
        known.add((signal_id, "executed"))
        added += 1
    return {"added": added, "skipped_existing": skipped, "missing_signal_file": missing_signal}


def backfill(days: int = 7) -> Dict[str, Any]:
    return reconcile_journal(days)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Executor trade ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="Rebuild rows from journal (idempotent)")
    bf.add_argument("--days", type=int, default=7)
    rc = sub.add_parser("reconcile", help="Add rows for one date")
    rc.add_argument("--date", type=str, required=True)
    args = parser.parse_args()

    if args.cmd == "backfill":
        result = backfill(args.days)
    else:
        result = reconcile_journal(0, date.fromisoformat(args.date)) or {"added": 0}
        # reconcile with days=0 reads nothing; do a targeted single-day pass
        end = date.fromisoformat(args.date)
        result = reconcile_journal(1, end)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
