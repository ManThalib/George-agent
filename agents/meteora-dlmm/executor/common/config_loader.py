"""Load and validate the executor config and safety rails."""

import os
from pathlib import Path
from typing import Any, Dict

from common.utils import load_json


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
        "mirror_capital_fraction": float(cfg.get("wallet", {}).get("mirror_capital_fraction", 1.0)),
        # Multi-wallet mirror: logical id -> public key. MAIN comes from
        # wallet.public_key above; mirrors are registered under
        # config wallet.mirrors = [{wallet_id, public_key}]. A signal's
        # wallet_id must be in this registry or the request is rejected
        # before any chain call (fail closed).
        "wallet_registry": _wallet_registry(cfg, public_key),
    }


def _wallet_registry(cfg: Dict[str, Any], main_public_key: str) -> Dict[str, str]:
    """Build the wallet_id -> public key registry from agent.config.json."""
    registry = {"main": main_public_key}
    for entry in cfg.get("wallet", {}).get("mirrors") or []:
        if not isinstance(entry, dict):
            continue
        wid = str(entry.get("wallet_id") or "").strip().lower()
        pubkey = str(entry.get("public_key") or "").strip()
        if not wid or not pubkey or wid == "main":
            continue
        registry[wid] = pubkey
    return registry
