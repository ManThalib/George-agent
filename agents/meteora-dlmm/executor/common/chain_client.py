"""Thin wrapper around the Node chain dispatcher."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict


CHAIN_DIR = Path(__file__).parent.parent / "chain"
DISPATCH_JS = CHAIN_DIR / "dispatch.js"
NODE = "node"


class ChainError(Exception):
    pass


def _run(req: Dict[str, Any]) -> Dict[str, Any]:
    if not DISPATCH_JS.exists():
        raise ChainError(f"chain dispatcher missing: {DISPATCH_JS}")

    env = os.environ.copy()
    proc = subprocess.run(
        [NODE, str(DISPATCH_JS)],
        input=json.dumps(req),
        text=True,
        capture_output=True,
        cwd=str(CHAIN_DIR),
        env=env,
        timeout=120,
    )

    if proc.returncode != 0:
        try:
            err = json.loads(proc.stdout)
        except Exception:
            err = proc.stdout
        raise ChainError(f"chain dispatcher failed ({proc.returncode}): {err}")

    try:
        response = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ChainError(f"chain dispatcher returned invalid JSON: {exc}\nstdout: {proc.stdout[:500]}") from exc

    if not response.get("ok"):
        stage = response.get("stage", "unknown")
        error = response.get("error", "unknown error")
        raise ChainError(f"chain dispatcher returned error at stage {stage}: {error}")

    return response


def simulate(req: Dict[str, Any]) -> Dict[str, Any]:
    req = {**req, "mode": "simulate"}
    return _run(req)


def send(req: Dict[str, Any]) -> Dict[str, Any]:
    req = {**req, "mode": "send"}
    return _run(req)
