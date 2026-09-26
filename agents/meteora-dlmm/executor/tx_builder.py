"""Thin wrapper around the Node chain CLI.

The chain CLI lives in executor/chain/ and speaks JSON on stdin/stdout.
This module builds requests, runs the CLI, and parses responses.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict


CHAIN_DIR = Path(__file__).parent / "chain"
NODE = "node"
DEX_JS = CHAIN_DIR / "dex.js"


class TxBuilderError(Exception):
    pass


def _run(req: Dict[str, Any]) -> Dict[str, Any]:
    """Run the chain CLI with the given request and return its JSON response."""
    if not DEX_JS.exists():
        raise TxBuilderError(f"chain CLI missing: {DEX_JS}")

    env = os.environ.copy()
    proc = subprocess.run(
        [NODE, str(DEX_JS)],
        input=json.dumps(req),
        text=True,
        capture_output=True,
        cwd=str(CHAIN_DIR),
        env=env,
        timeout=120,
    )

    if proc.returncode != 0:
        # stdout may contain the JSON error; stderr has diagnostics.
        try:
            err = json.loads(proc.stdout)
        except Exception:
            err = proc.stdout
        raise TxBuilderError(f"chain CLI failed ({proc.returncode}): {err}")

    try:
        response = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TxBuilderError(f"chain CLI returned invalid JSON: {exc}\nstdout: {proc.stdout[:500]}") from exc

    if not response.get("ok"):
        stage = response.get("stage", "unknown")
        error = response.get("error", "unknown error")
        raise TxBuilderError(f"chain CLI returned error at stage {stage}: {error}")

    return response


def _request_from_signal(mode: str, signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    action = signal["action"]
    slippage = min(int(signal.get("max_slippage_bps", rails.get("max_slippage_bps", 100))), int(rails.get("max_slippage_bps", 100)))

    base: Dict[str, Any] = {
        "mode": mode,
        "rpc_url": cfg["rpc_https_url"],
        "wallet_public_key": cfg["wallet_public_key"],
        "max_slippage_bps": slippage,
        "priority_fee_cap_sol": rails.get("priority_fee_cap_sol", 0.0005),
    }

    if action == "swap":
        base.update({
            "dex": "jupiter",
            "action": "swap",
            "input_mint": signal["input_mint"],
            "output_mint": signal["output_mint"],
            "amount": signal["amount"],
            "exact_out": signal.get("exact_out", False),
            "max_price_impact_pct": rails.get("max_price_impact_pct", 1.5),
        })
        return base

    dex = signal.get("dex") or "meteora"
    base.update({
        "dex": dex,
        "action": action if action in {"open", "close", "claim"} else "close" if action == "claim_rewards" or action == "claim_fees" else "close",
        "pool_address": signal["pool_address"],
        "position_id": signal.get("position_id"),
        "bin_range": signal.get("bin_range"),
        "liquidity": signal.get("liquidity"),
    })
    return base


def simulate(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Build and simulate a transaction for the given signal."""
    req = _request_from_signal("simulate", signal, cfg, rails)
    return _run(req)


def send(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    """Build, simulate, sign and send a transaction for the given signal."""
    req = _request_from_signal("send", signal, cfg, rails)
    return _run(req)
