"""PnL report over the executor trade ledger.

Reads ledger/ledger.jsonl and prints per-day realized position change,
gas spent, rent recovered, and net. USD figures use signal-supplied
position_usd where present; raw-amount rows without usable prices are
reported as unpriced, never guessed.

Usage:
    python3 report.py --date 2026-09-30
    python3 report.py --days 7
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent))
import ledger


def _usd_of_open(row: Dict[str, Any]) -> float:
    """Best-effort USD notional of an open: signal position_usd first."""
    try:
        return float(row.get("position_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    per_day: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "opens": 0, "closes": 0, "claims": 0, "swaps": 0,
        "opened_usd": 0.0, "unpriced": 0,
        "gas_sol": 0.0, "gas_unavailable": 0,
        "rent_recovered_sol": 0.0,
        "verify_failed": 0,
    })
    for row in rows:
        day = str(row.get("timestamp_utc", ""))[:10]
        d = per_day[day]
        action = row.get("action")
        if action == "open":
            d["opens"] += 1
            usd = _usd_of_open(row)
            if usd:
                d["opened_usd"] += usd
            elif row.get("amounts"):
                d["unpriced"] += 1
            if row.get("verify_ok") is False:
                d["verify_failed"] += 1
        elif action == "close":
            d["closes"] += 1
            rent = (row.get("rent_recovered") or {}).get("rent_lamports")
            if rent is not None:
                d["rent_recovered_sol"] += rent / 1e9
        elif action in ("claim_fees", "claim_rewards", "claim"):
            d["claims"] += 1
        elif action == "swap":
            d["swaps"] += 1
        for gas_key in ("gas", "gas_close"):
            gas = row.get(gas_key) or {}
            lamports = gas.get("fee_lamports")
            if lamports is not None:
                d["gas_sol"] += lamports / 1e9
            elif gas:
                d["gas_unavailable"] += 1
    return dict(per_day)


def render(per_day: Dict[str, Dict[str, Any]]) -> str:
    lines = [f"{'day':<12} {'opens':>5} {'closes':>6} {'claims':>6} {'swaps':>5} "
             f"{'opened_usd':>10} {'gas_SOL':>8} {'rent_SOL':>8} {'verify_fail':>11}"]
    total_gas = 0.0
    total_rent = 0.0
    for day in sorted(per_day):
        d = per_day[day]
        total_gas += d["gas_sol"]
        total_rent += d["rent_recovered_sol"]
        lines.append(
            f"{day:<12} {d['opens']:>5} {d['closes']:>6} {d['claims']:>6} {d['swaps']:>5} "
            f"{d['opened_usd']:>10.2f} {d['gas_sol']:>8.6f} {d['rent_recovered_sol']:>8.6f} "
            f"{d['verify_failed']:>11}"
        )
    lines.append(f"\ntotals: gas {total_gas:.6f} SOL, rent recovered {total_rent:.6f} SOL, "
                 f"gas net {(total_rent - total_gas):+.6f} SOL")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ledger PnL report")
    parser.add_argument("--date", type=str, help="Report one UTC day (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=7, help="Report the last N UTC days")
    args = parser.parse_args()

    rows = ledger.load_rows()
    if not rows:
        print("ledger empty — run: python3 ledger/ledger.py backfill --days 7")
        return 1
    if args.date:
        rows = ledger.rows_for_date(rows, args.date)
    else:
        cutoff = (date.today() - timedelta(days=args.days - 1)).isoformat()
        rows = [r for r in rows if str(r.get("timestamp_utc", ""))[:10] >= cutoff]
    if not rows:
        print("no rows in range")
        return 1
    print(render(summarize(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
