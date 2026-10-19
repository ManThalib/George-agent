#!/usr/bin/env python3
"""Single source of truth for *which* agent.config.json governs execution.

Why this module exists
----------------------
Two copies of ``agent.config.json`` were in play:

  workspace: /root/.openclaw-devdash/workspace/agents/George-agent/
             agents/meteora-dlmm/config/agent.config.json
  live:      /data/.openclaw/workspace-agents/george/
             agents/meteora-dlmm/config/agent.config.json

``run_pipeline.sh`` gated on the *workspace* copy while
``common/config_loader.py`` loaded the *live* copy. On 2026-10-18 the two
disagreed (``dry_run`` vs ``auto``): the pipeline log printed
"(dry_run)" while the executor was live-capable. The gate was decorative.

Policy (fail closed, always)
----------------------------
1. The live config must exist and parse. Otherwise refuse.
2. ``agent.mode`` must be a known value. Otherwise refuse.
3. If the workspace copy exists, its mode must EQUAL the live mode.
   Drift refuses; we never guess which copy the operator meant.
3b. If both copies name a ``wallet.public_key``, the values must match.
   The wallet rotated on 2026-10-19 and the live copy still pointed at the
   retired address while the workspace copy pointed at the new one: the
   executor would have signed with one key while every report, position scan
   and capital figure tracked another. Same failure class as the mode drift
   above, so it gets the same answer — refuse, never guess.
4. A non-``dry_run`` mode requires a signer in the process environment.
   Otherwise refuse, because every send would fail after signals were
   already emitted.
5. ``require_dry_run`` (used by ``run_pipeline.sh``) refuses ANY mode other
   than ``dry_run`` even when a signer is present. The scheduled pipeline is
   a readiness/reporting runner; it is not an execution vehicle. Allowing
   ``auto`` here would mean an unattended timer could trade on the strength
   of a config edit alone, with no owner in the loop. Live execution must go
   through an explicitly authorized path, not through this runner.

Refusal is an exit code, never a warning: callers must not start the
dispatcher on a refusal.

CLI
---
    python3 config_gate.py [--signer-present 0|1] [--require-dry-run] [--json]

Exit 0 = allowed to dispatch, 1 = refuse, 2 = usage/internal error.
Prints a one-line reason. Never prints secrets or config contents.
"""

import argparse
import json
import os
import sys
from pathlib import Path

LIVE_CONFIG_PATH = Path(
    "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/config/agent.config.json"
)
WORKSPACE_CONFIG_PATH = Path(
    "/root/.openclaw-devdash/workspace/agents/George-agent"
    "/agents/meteora-dlmm/config/agent.config.json"
)

KNOWN_MODES = ("dry_run", "auto")

# Overrides exist for tests and for relocating the live tree; the defaults
# are the production paths and must stay pointed at the executor's file.
ENV_LIVE = "GEORGE_CONFIG_PATH"
ENV_WORKSPACE = "GEORGE_WORKSPACE_CONFIG_PATH"


def live_config_path() -> Path:
    """Path the *executor* reads. This is authoritative."""
    override = os.environ.get(ENV_LIVE, "").strip()
    return Path(override) if override else LIVE_CONFIG_PATH


def workspace_config_path() -> Path:
    """Path the pipeline gate historically read. Only used for drift checks."""
    override = os.environ.get(ENV_WORKSPACE, "").strip()
    return Path(override) if override else WORKSPACE_CONFIG_PATH


def read_mode(path) -> str:
    """Return ``agent.mode`` from a config file.

    Raises ValueError with a path-only message on any problem; callers turn
    that into a refusal. The message never includes file contents.
    """
    path = Path(path)
    if not path.exists():
        raise ValueError(f"config missing: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"config unreadable: {path} ({type(exc).__name__})")
    agent = data.get("agent")
    if not isinstance(agent, dict):
        raise ValueError(f"config has no agent object: {path}")
    mode = agent.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError(f"agent.mode missing or not a string: {path}")
    return mode.strip()


def read_public_key(path) -> str:
    """Return ``wallet.public_key`` from a config file (``""`` if absent).

    A missing wallet block is not itself a refusal: dry_run needs no signer
    and the scanner has its own address source. A *disagreement* is, because
    that means reports and signatures describe different wallets.
    """
    path = Path(path)
    if not path.exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ""
    wallet = data.get("wallet")
    if not isinstance(wallet, dict):
        return ""
    key = wallet.get("public_key")
    return key.strip() if isinstance(key, str) else ""


def check_gate(signer_present, live=None, workspace=None, require_dry_run=False):
    """Decide whether the dispatcher may start.

    ``signer_present`` is a bool (or a truthy/falsey value). ``require_dry_run``
    makes the caller's own policy stricter than the config: any non-dry_run
    mode is refused even if a signer exists. Returns ``(allowed, reason)``
    where reason is a single line suitable for a log.
    """
    live = Path(live) if live else live_config_path()
    workspace = Path(workspace) if workspace else workspace_config_path()

    try:
        live_mode = read_mode(live)
    except ValueError as exc:
        return False, f"REFUSING: {exc}"

    if live_mode not in KNOWN_MODES:
        return False, (
            f"REFUSING: agent.mode={live_mode!r} in {live} is not one of "
            f"{list(KNOWN_MODES)}"
        )

    if workspace.exists():
        try:
            ws_mode = read_mode(workspace)
        except ValueError as exc:
            return False, f"REFUSING: workspace copy unusable: {exc}"
        if ws_mode != live_mode:
            return False, (
                f"REFUSING: agent.mode disagrees — executor reads "
                f"{live_mode!r} from {live} but the workspace copy says "
                f"{ws_mode!r} ({workspace}). Align them; refusing to guess."
            )
        live_key = read_public_key(live)
        ws_key = read_public_key(workspace)
        if live_key and ws_key and live_key != ws_key:
            # Never echo either address in full: a mismatch report only needs
            # to identify that they differ, not publish both keys.
            return False, (
                f"REFUSING: wallet.public_key disagrees — executor tracks "
                f"{live_key[:4]}…{live_key[-4:]} from {live} but the "
                f"workspace copy tracks {ws_key[:4]}…{ws_key[-4:]} "
                f"({workspace}). Align them; refusing to guess."
            )

    if live_mode != "dry_run" and require_dry_run:
        return False, (
            f"REFUSING: agent.mode={live_mode!r} but this runner requires "
            f"dry_run. The scheduled pipeline is a readiness/reporting runner, "
            f"not an execution path; a live transition needs separate owner "
            f"authorization and its own supervised entrypoint."
        )

    if live_mode != "dry_run" and not signer_present:
        return False, (
            f"REFUSING: agent.mode={live_mode!r} but no signer is present "
            f"in the service environment (SOLANA_AGENT_WALLET unset). "
            f"Every send would fail after signals were emitted."
        )

    drift = "no workspace copy" if not workspace.exists() else "modes agree"
    return True, f"OK: agent.mode={live_mode!r} from {live} ({drift})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed gate for George dispatcher start"
    )
    parser.add_argument(
        "--signer-present",
        default="0",
        help="1 if SOLANA_AGENT_WALLET is present in the environment, else 0",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--require-dry-run",
        action="store_true",
        help="refuse any mode other than dry_run, even with a signer present",
    )
    parser.add_argument("--live", default=None, help="override live config path")
    parser.add_argument(
        "--workspace", default=None, help="override workspace config path"
    )
    args = parser.parse_args(argv)

    signer = str(args.signer_present).strip() not in ("", "0", "false", "False")
    allowed, reason = check_gate(
        signer,
        live=args.live,
        workspace=args.workspace,
        require_dry_run=args.require_dry_run,
    )

    if args.json:
        mode = ""
        try:
            mode = read_mode(args.live or live_config_path())
        except ValueError:
            mode = "unknown"
        print(json.dumps({"allowed": allowed, "mode": mode, "reason": reason}))
    else:
        print(reason, file=sys.stdout if allowed else sys.stderr)
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
