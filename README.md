# George Agent — DLMM Executor

Autonomous Solana LP position executor for Meteora DLMM, Raydium CLMM, and Orca Whirlpools.

## Overview

George receives signal files from the analyst sub-agent (Sheldon), validates them against safety rails, and executes transactions in the configured mode.

- **Entry point**: `agents/meteora-dlmm/executor/dispatcher.py`
- **Signals**: JSON files in `agents/meteora-dlmm/signals/pending/`; handled signals move to
  `signals/processed/`, opens/adds that fail on-chain self-verification go to
  `signals/failed_verify/` (closes only warn — execution already happened)
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

```text
agents/meteora-dlmm/
├── config/
│   └── agent.config.json          # wallet, RPC, mode, mirrors
├── executor/
│   ├── dispatcher.py              # main signal dispatch loop (+ capital-refresh chain)
│   ├── chain/                     # Node chain layer (dispatch.js, tx.js, jupiter.js, prices.js)
│   ├── common/                    # shared executor helpers
│   │   ├── signal_validator.py    # core validation (all actions)
│   │   ├── rails_loader.py        # execution_limits.json + sheldon policy + SAFETY_RAILS.md
│   │   ├── config_loader.py
│   │   ├── journal.py
│   │   ├── approvals.py           # confirm_each pending store
│   │   ├── dust_queue.py          # swap_to_usdc review queue
│   │   ├── mirror.py              # multi-wallet mirror legs + drift
│   │   ├── chain_client.py
│   │   ├── exposure_guard.py
│   │   └── utils.py
│   └── dexes/                     # per-DEX builders and validators
│       ├── meteora/               # builder.py, validator.py, position.py, chain.js
│       ├── orca/
│       └── raydium/
├── signals/
│   ├── pending/                   # incoming signals from Sheldon
│   ├── processed/                 # handled signals (audit trail)
│   └── failed_verify/             # opens/adds that failed on-chain self-verification
├── approvals/                     # confirm_each: pending/, approved/, rejected/, failed_verify/
├── data/dust_swaps/               # swap_to_usdc review queue: pending/, reviewed/
├── journal/                       # daily JSONL decision log
├── ledger/                        # trade ledger (ledger.py, report.py, ledger.jsonl)
├── state/                         # exposure_state.json, capital_reentry.json,
│                                  # mirror_sync.json, dispatcher.lock
├── execution_limits.json          # machine-readable execution rails
└── SAFETY_RAILS.md                # human-readable safety rules
```

> Note: the executor resolves `agents/meteora-dlmm/` to the absolute deploy path
> (`/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/`) in
> `dispatcher.py`, `common/*`, `config_loader.py`, and `ledger/ledger.py`.
> Relative paths above map to that root at runtime.

## Supported Actions

- `open` — open a new LP position (requires `pool_address`, `bin_range`, `liquidity`,
  `position_usd`, `score` ≥ `min_open_score`; range must contain the pool's live tick,
  Meteora `bin_step` must be in `allowed_bin_steps`)
- `close` — close a position (requires `position_id`; de-risking, bypasses range/exposure rails)
- `claim_fees` / `claim_rewards` — claim accrued fees/rewards (requires `position_id`;
  Raydium computes real pending value and skips below `claim_min_usd` = $1.00)
- `add_liquidity` — add to an existing position (requires `position_id`, `bin_range`,
  `liquidity`, `position_usd`; guarded like an open: exposure + range rails apply)
- `remove_liquidity` — partial removal (requires `position_id`, `bps` 1–9999;
  de-risking like close; `bps` ≥ 10000 is rejected — use `close`)
- `swap` — Jupiter swap (requires `input_mint`, `output_mint`, `amount`)
- `swap_to_usdc` — dust sweep to USDC (requires `mint`, `symbol`, `decimals`,
  `amount_raw`, `amount_ui`, `value_usd`, `reason`; auto-executes only in `auto` mode,
  otherwise queued in `data/dust_swaps/pending/` for review:
  `dispatcher.py --list-dust-swaps` / `--sweep-dust-swaps`)

All non-swap signals carry `signal_id`, `action`, `created_at`; signals older than
`signal_max_age_seconds` (300s) are rejected as stale. `position_id` is required for
`close`, `claim_*`, `add_liquidity`, `remove_liquidity`.

## Safety

- Kill switch: create `agents/meteora-dlmm/KILL` to halt all new transactions
- Every transaction logs the hash of `SAFETY_RAILS.md` in `journal/YYYY-MM-DD.jsonl`
- Max exposure, drawdown, slippage, and position-size rails are enforced
- 3 consecutive failures → self-pause + critical alert
- On-chain self-verification: opens/adds that fail verification move to
  `signals/failed_verify/` + owner alert; closes only warn (execution already happened)
- Claim gating: computed claim value below `claim_min_usd` is skipped, not sent
- Capital-refresh chain: after a `swap` executes, the dispatcher rescans the wallet,
  re-runs Sheldon, and processes follow-up signals — bounded by 8 re-entries/hour
  and a chain depth of 3, serialized by `state/dispatcher.lock`
- Multi-wallet mirror (when `wallet.mirrors` is configured): MAIN leg runs first;
  mirror opens run only if MAIN succeeded, mirror closes/claims run regardless;
  divergence is recorded in `state/mirror_sync.json` and surfaced as drift

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
