"""Single source of truth for every filesystem path the executor touches.

Before this module the live tree was re-typed in eight modules, spelled two
different ways (`workspace-agents/george` and `workspace-agents-george`) in
the same file, and none of the non-signal paths honoured the environment
overrides the dispatcher already supported. The consequences were real: the
watchdog watched directories the dispatcher never wrote to, and the test
suite could only run on the production host.

Resolution order for the agent tree (highest priority first):

1. ``GEORGE_AGENT_ROOT`` — explicit override, used by tests and by any
   relocation of the live tree.
2. ``/data/.openclaw/workspace-agents/george/agents/meteora-dlmm`` — the
   live tree, when it exists (production behaviour is unchanged).
3. This checkout's ``agents/meteora-dlmm`` — so the suite runs anywhere.

Missy's scan directories and the sibling agents live outside this repo, so
they have no checkout fallback: an absent directory is reported as absent by
the callers that read scans, never invented here.
"""

import os
from pathlib import Path

_LIVE_AGENT_ROOT = Path(
    "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm"
)
_REPO_AGENT_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_AGENTS_ROOT = "/root/.openclaw-devdash/workspace/agents"
_DEFAULT_MISSY_DATA_ROOT = "/data/missy-data"

ENV_AGENT_ROOT = "GEORGE_AGENT_ROOT"
ENV_AGENTS_ROOT = "AGENTS_ROOT"
ENV_MISSY_DATA_ROOT = "GEORGE_MISSY_DATA_ROOT"
ENV_CONFIG = "GEORGE_CONFIG_PATH"
ENV_WORKSPACE_CONFIG = "GEORGE_WORKSPACE_CONFIG_PATH"
ENV_SHELDON_POLICY = "GEORGE_SHELDON_POLICY_PATH"


def _env_path(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def agent_root() -> Path:
    """The executor's own tree (signals, state, journal, config, rails)."""
    override = _env_path(ENV_AGENT_ROOT)
    if override:
        return override
    if _LIVE_AGENT_ROOT.is_dir():
        return _LIVE_AGENT_ROOT
    return _REPO_AGENT_ROOT


def agents_root() -> Path:
    """Directory holding the sibling agents (Missy, Sheldon, ...)."""
    override = _env_path(ENV_AGENTS_ROOT)
    return override or Path(_DEFAULT_AGENTS_ROOT)


def missy_data_root() -> Path:
    """Missy's scan output root (pool / position / wallet screens)."""
    override = _env_path(ENV_MISSY_DATA_ROOT)
    return override or Path(_DEFAULT_MISSY_DATA_ROOT)


def config_path() -> Path:
    """The config file the *executor* reads. Authoritative."""
    override = _env_path(ENV_CONFIG)
    return override or agent_root() / "config" / "agent.config.json"


def workspace_config_path() -> Path:
    """The config copy the pipeline gate historically read (drift check only)."""
    override = _env_path(ENV_WORKSPACE_CONFIG)
    return override or (
        Path(_DEFAULT_AGENTS_ROOT) / "George-agent" / "agents" / "meteora-dlmm"
        / "config" / "agent.config.json"
    )


# --- agent-tree children --------------------------------------------------
def signals_dir() -> Path:
    return agent_root() / "signals"


def journal_dir() -> Path:
    return agent_root() / "journal"


def state_dir() -> Path:
    return agent_root() / "state"


def approvals_dir() -> Path:
    return agent_root() / "approvals"


def dust_swaps_dir() -> Path:
    return agent_root() / "data" / "dust_swaps"


def ledger_path() -> Path:
    return agent_root() / "ledger" / "ledger.jsonl"


def kill_file() -> Path:
    return agent_root() / "KILL"


def rails_path() -> Path:
    return agent_root() / "SAFETY_RAILS.md"


def execution_limits_path() -> Path:
    return agent_root() / "execution_limits.json"


def sheldon_policy_path() -> Path:
    override = _env_path(ENV_SHELDON_POLICY)
    if override:
        return override
    return agents_root() / "sheldon" / "scoring" / "sheldon_policy.json"


# --- Missy scan families (read-only inputs) -------------------------------
def wallet_scan_dir() -> Path:
    return missy_data_root() / "wallet_screens"


def position_scan_dir() -> Path:
    return missy_data_root() / "position_scans"


def pool_scan_dir() -> Path:
    return missy_data_root() / "pool_screens"
