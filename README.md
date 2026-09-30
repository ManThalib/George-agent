# George Agent — DLMM Executor

Autonomous Solana LP position executor for Meteora DLMM, Raydium CLMM, and Orca Whirlpools.

## Overview

George receives signal files from the analyst sub-agent (Sheldon), validates them against safety rails, and executes transactions in the configured mode.

- **Entry point**: `agents/meteora-dlmm/executor/dispatcher.py`
- **Signals**: JSON files in `agents/meteora-dlmm/signals/pending/`
- **Safety**: `agents/meteora-dlmm/SAFETY_RAILS.md` + `execution_limits.json`
- **Journal**: `agents/meteora-dlmm/journal/YYYY-MM-DD.jsonl`

## Modes

| Mode | Behavior |
|---|---|
| `dry_run` | Simulate, log, sign nothing (default) |
| `confirm_each` | Pause every transaction for explicit approval |
| `auto` | Execute automatically when all rails pass |

Mode is set in `agents/meteora-dlmm/config/agent.config.json`.

## Repository Layout

```textnagents/meteora-dlmm/
├── config/
│   └── agent.config.json          # wallet, RPC, mode
├── executor/
│   ├── dispatcher.py              # main signal dispatch loop
│   ├── common/                    # shared executor helpers
│   │   ├── signal_validator.py
│   │   ├── rails_loader.py
│   │   ├── journal.py
│   │   ├── approvals.py
│   │   ├── chain_client.py
│   │   ├── exposure_guard.py
│   │   └── ...
│   └── dexes/                     # per-DEX builders and validators
│       ├── meteora/
│       ├── orca/
│       └── raydium/
├── signals/
│   ├── pending/                   # incoming signals from Sheldon
│   └── processed/                   # handled signals (audit trail)
├── journal/                       # daily JSONL decision log
├── state/                         # runtime state (locks, capital reentry)
├── execution_limits.json          # machine-readable execution rails
└── SAFETY_RAILS.md                # human-readable safety rules
```

## Supported Actions

- `open` — open a new LP position
- `close` — close an position
- `claim_fees` — claim accrued fees
- `swap` — Jupiter swap (if enabled)

## Safety

- Kill switch: create `agents/meteora-dlmm/KILL` to halt all new transactions
- Every transaction logs the hash of `SAFETY_RAILS.md` + config
- Max exposure, drawdown, slippage, and position-size rails are enforced
- 3 consecutive failures → self-pause + critical alert

## Tests

```bash
python3 -m unittest discover -s tests
```

## Workspace Files

This repository is George's entire workspace. Agent identity and memory files are included by design:

- `AGENTS.md` — workspace conventions
- `IDENTITY.md` — agent identity
- `SOUL.md` — personality/tone
- `USER.md` — durable user directives
- `memory/` — session notes
