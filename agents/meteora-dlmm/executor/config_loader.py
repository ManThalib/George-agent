"""Load and validate the executor config and safety rails."""

import os
from pathlib import Path
from typing import Any, Dict

from common import load_json


CONFIG_PATH = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/config/agent.config.json")
RAILS_PATH = Path("/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/SAFETY_RAILS.md")


class ConfigError(Exception):
    pass


def load_config() -> Dict[str, Any]:
    """Load and validate the executor config.

    Raises ConfigError if required fields are missing.
    """
    if not CONFIG_PATH.exists():
        raise ConfigError(f"config missing: {CONFIG_PATH}")
    cfg = load_json(CONFIG_PATH)
    wallet = cfg.get("wallet", {})
    rpc = cfg.get("rpc", {})
    public_key = os.environ.get("SOLANA_PUBLIC_WALLET", wallet.get("public_key", "")).strip()
    rpc_https = os.environ.get("SOLANA_RPC_URL", rpc.get("https_url", "")).strip()
    if not public_key:
        raise ConfigError("wallet.public_key is required (set SOLANA_PUBLIC_WALLET or agent.config.json)")
    if not rpc_https:
        raise ConfigError("rpc.https_url is required (set SOLANA_RPC_URL or agent.config.json)")
    mode = cfg.get("agent", {}).get("mode", "dry_run")
    return {
        "wallet_public_key": public_key,
        "mode": mode,
        "rpc_https_url": rpc_https,
        "signal_max_age_seconds": cfg.get("analyst_feed", {}).get("signal_max_age_seconds", 300),
        "heartbeat_interval_seconds": cfg.get("analyst_feed", {}).get("heartbeat_interval_seconds", 60),
        "commitment": cfg.get("rpc", {}).get("commitment", "confirmed"),
    }
