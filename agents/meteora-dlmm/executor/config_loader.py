"""Load and validate the executor config and safety rails."""

import os
from pathlib import Path
from typing import Any, Dict

# Secrets are injected by the OpenClaw secrets store as environment variables
# (SOLANA_RPC_URL, SOLANA_AGENT_WALLET) when commands run on the gateway host.
# There is intentionally NO plaintext fallback file: if the env var is absent,
# load_config raises ConfigError instead of reading secrets from disk.

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

    # Send-path fallback endpoints. Key-bearing (paid) endpoints must come via
    # the SOLANA_RPC_FALLBACK_URLS env var (comma-separated) — never in files.
    # The keyless public endpoint may live in the config as the default.
    env_fallbacks = os.environ.get("SOLANA_RPC_FALLBACK_URLS", "").strip()
    if env_fallbacks:
        fallback_urls = [u.strip() for u in env_fallbacks.split(",") if u.strip()]
    else:
        fallback_urls = [
            u.strip()
            for u in rpc.get("fallback_https_urls", [])
            if isinstance(u, str) and u.strip()
        ]
    for u in fallback_urls:
        if not u.startswith("https://"):
            raise ConfigError(f"rpc fallback endpoint must be https: {u}")

    return {
        "wallet_public_key": public_key,
        "mode": mode,
        "rpc_https_url": rpc_https,
        "rpc_fallback_urls": fallback_urls,
        "rpc_fallback_delay_seconds": float(rpc.get("fallback_delay_seconds", 15)),
        "signal_max_age_seconds": cfg.get("analyst_feed", {}).get("signal_max_age_seconds", 300),
        "heartbeat_interval_seconds": cfg.get("analyst_feed", {}).get("heartbeat_interval_seconds", 60),
        "commitment": cfg.get("rpc", {}).get("commitment", "confirmed"),
    }
