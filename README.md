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
│   └── agent.config.json          # mode, mirrors (no wallet identity, no RPC key)
├── executor/
│   ├── dispatcher.py              # main signal dispatch loop (+ capital/close-refresh chains)
│   ├── chain/                     # Node chain layer (dispatch.js, tx.js, jupiter.js, prices.js,
│   │                              #   common.js, amounts.js) + tools/activebin.mjs
│   ├── common/                    # shared executor helpers
│   │   ├── paths.py               # single owner of every filesystem path (env-overridable)
│   │   ├── solana.py              # mints, token programs, DEX program ids
│   │   ├── chain_request.py       # shared request envelope + slippage clamp
│   │   ├── circuit_breaker.py     # consecutive-failure self-pause
│   │   ├── bin_step_rail.py       # Meteora bin_step predicate
│   │   ├── signal_validator.py    # core validation (all actions)
│   │   ├── config_gate.py         # fail-closed start gate (mode/wallet drift, signer)
│   │   ├── rails_loader.py        # execution_limits.json + sheldon policy + SAFETY_RAILS.md
│   │   ├── config_loader.py
│   │   ├── live_preflight.py      # live SOL/token balance preflight for opens
│   │   ├── journal.py
│   │   ├── approvals.py           # confirm_each pending store
│   │   ├── dust_queue.py          # swap_to_usdc review queue
│   │   ├── mirror.py              # multi-wallet mirror legs + drift
│   │   ├── chain_client.py
│   │   ├── exposure_guard.py
│   │   └── utils.py
│   └── dexes/                     # per-DEX builders and validators
│       ├── meteora/               # builder.py, validator.py, chain.js
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
├── state/                         # exposure_state.json, capital_reentry.json, mirror_sync.json,
│                                  # circuit_breaker.json, dispatcher.lock
├── execution_limits.json          # machine-readable execution rails
└── SAFETY_RAILS.md                # human-readable safety rules
```

> Paths: `common/paths.py` is the single owner. Resolution order is
> `GEORGE_AGENT_ROOT` → the live deploy tree
> (`/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/`, used when it
> exists) → this checkout. Missy's scan directories come from
> `GEORGE_MISSY_DATA_ROOT` (default `/data/missy-data`). No module hardcodes a tree
> any more, which is why the test suite runs off-host.

## Supported Actions

- `open` — open a new LP position (requires `pool_address`, `bin_range`, `liquidity`,
  `position_usd`, `score` ≥ `min_open_score` = **90.0**, clamped up to the 55.0 adaptive
  floor; the range must contain the pool's live tick; Meteora's on-chain `bin_step` must
  be inside `bin_step_rule` = **[4..400]**)
- `close` — close a position (requires `position_id`; de-risking, bypasses range/exposure rails)
- `claim_fees` / `claim_rewards` — claim accrued fees/rewards (requires `position_id`;
  Raydium computes real pending value and skips below `claim_min_usd` = $1.00)
- `add_liquidity` — add to an existing position (requires `position_id`, `bin_range`,
  `liquidity`, `position_usd`; guarded like an open: exposure + range rails apply).
  **Meteora only** — see the DEX coverage note below
- `remove_liquidity` — partial removal (requires `position_id`, `bps` 2500–7500;
  de-risking like close; `bps` ≥ 10000 is rejected — use `close`; remaining value
  must stay ≥ `min_position_usd`; 6h mutation cooldown applies). **Meteora only**
- `rebalance` — close range + reopen same pool (requires `position_id`, `bin_range`,
  `liquidity`, `position_usd`; guarded on net exposure like an open)
- `rotate` — close one position + open another pool (same required fields and
  net-exposure guard as `rebalance`; triggers the close-refresh chain)
- `swap` — Jupiter swap (requires `input_mint`, `output_mint`, `amount`)
- `swap_to_usdc` — dust sweep to USDC (requires `mint`, `symbol`, `decimals`,
  `amount_raw`, `amount_ui`, `value_usd`, `reason`; auto-executes only in `auto` mode,
  otherwise queued in `data/dust_swaps/pending/` for review:
  `dispatcher.py --list-dust-swaps` / `--sweep-dust-swaps`)

All non-swap signals carry `signal_id`, `action`, `created_at`; signals older than
`signal_max_age_seconds` (300s) are rejected as stale. `position_id` is required for
`close`, `claim_*`, `add_liquidity`, `remove_liquidity`, `rebalance`, `rotate`.
Mutations accept an optional `side` (`bidirectional` default, or `x`/`y`): the
**un-chosen side must be zero**, since the instruction acts on the declared side only;
mutations on the same `position_id` are limited to one per 6h (`mutation_cooldown_hours`).

**DEX coverage:** Meteora supports every action. Orca and Raydium support `open`,
`close` and `claim`; an `add_liquidity` / `remove_liquidity` signal for them is
**refused** by the chain dispatcher (it cannot report success without touching the
chain) — implementing or rejecting them earlier is tracked in `DEFERRED.md` §2.

## Safety

- Kill switch: create `agents/meteora-dlmm/KILL` to halt all new transactions (`--once` exits 75)
- **Failure breaker (2026-10-30):** 3 consecutive execution failures (or 5 consecutive
  RPC errors) → self-pause, critical alert, nothing processed (`--once` exits 76);
  sticky until `dispatcher.py --resume`. Only a successful execution breaks the streak
- Every decision logs the hash of `SAFETY_RAILS.md` in `journal/YYYY-MM-DD.jsonl`
  (recomputed whenever the file changes)
- Max exposure, daily loss, drawdown, slippage, range-width, remove-bps, cooldown and
  position-size rails are enforced; a missing `execution_limits.json` can only make
  the fallback stricter
- No wallet identity in the repo: address + keypair come from the OpenClaw secrets store
- On-chain self-verification: opens/adds that fail verification move to
  `signals/failed_verify/` + owner alert, journal as `executed_unverified`, and take
  no exposure slot; closes only warn (execution already happened)
- Claim gating: computed claim value below `claim_min_usd` is skipped, not sent;
  claims never close the tracked position
- Live preflight: opens/adds/rebalances/rotates re-check live SOL + token balances
  before simulating (`INSUFFICIENT_SOL_FOR_OPEN` / `INSUFFICIENT_TOKEN_BALANCE`,
  fail-closed when unreadable)
- Capital-refresh chain: after a `swap` executes, the dispatcher rescans the wallet,
  re-runs Sheldon, and processes follow-up signals — bounded by 8 re-entries/hour
  and a chain depth of 3, serialized by `state/dispatcher.lock`
- Close-refresh chain: after `close`/`rotate`/`rebalance`, the dispatcher rescans
  positions + wallet and re-runs Sheldon (20/hour cap); a close still present in the
  fresh scan blocks re-entry to avoid a close→scan→close loop
- Scheduling: closes outrank preps outrank opens; prep swaps capped at 1/wake;
  each wake reconciles tracked exposure against the authoritative position scan
- Multi-wallet mirror (when `wallet.mirrors` is configured): MAIN leg runs first;
  mirror opens run only if MAIN succeeded, mirror closes/claims run regardless;
  divergence is recorded in `state/mirror_sync.json` and surfaced as drift

## Health check, pauses, and recovery

```bash
cd agents/meteora-dlmm/executor
python3 dispatcher.py --check     # mode, wallet/signer/rpc presence (no valid config needed)
python3 dispatcher.py --once      # one cycle: 0 healthy, 1 failed, 75 KILL, 76 self-paused
python3 dispatcher.py --resume    # clear a self-pause after fixing the cause
```

`--check` never prints an endpoint or an address in full. `--resume` raises no rail:
the same threshold trips again after three more failures. Full procedure in
`DEFERRED.md` §8.

## Tests

```bash
./run_tests.sh
```

Runs the executor unit tests, the root integration tests, and the chain layer's
JS tests. The Python suites are hermetic (no `/data`, no live config); tests
that need the production host skip themselves with a reason.

```bash
python3 -m unittest discover -s tests                        # root only
cd agents/meteora-dlmm/executor && python3 -m unittest discover -s common
cd agents/meteora-dlmm/executor && npm test                  # chain layer (JS)
```

## Owner checklist

`DEFERRED.md` lists what is blocked on you: the OpenClaw secrets (wallet address,
keypair, RPC URL), the Orca/Raydium add/remove decision, staging verification of
the JS chain layer, and the still-committed runtime state. No wallet identity is
stored in this repository by design; `dispatcher.py --check` reports what the
service environment is missing.

## Workspace Files

This repository is George's entire workspace. Agent identity and memory files are included by design:

- `AGENTS.md` — workspace conventions
- `SOUL.md` — operating contract (identity, truths, scope)
- `IDENTITY.md` — agent identity
- `USER.md` — durable user directives
- `DEFERRED.md` — owner checklist: secrets, deferred features, staging verification
- `memory/` — session notes
- `run_tests.sh` — one runner for all three suites
