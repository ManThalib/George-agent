# DEFERRED — owner checklist

Open items from the 2026-10-29/30 audit that need **you** (or OpenClaw), not more
code. Each entry says what is missing, why it was not done, and exactly what to
change. Nothing here weakens the executor: every item is either blocked on a
secret, a policy decision, or a staging environment.

*Last synced with the code: 2026-10-30 (same day the failure breaker landed,
`SAFETY_RAILS.md` was re-synced, and all wallet identities were removed).*

Status key: **BLOCKED** = cannot proceed without you · **OPEN** = decided, not yet built

---

## 1. Wallet + RPC secrets (BLOCKED — OpenClaw)

**State:** the executor has **no wallet identity in the repo** (owner instruction
2026-10-30). `agents/meteora-dlmm/config/agent.config.json` has
`wallet.public_key: ""` with a note; the archived rotation config was scrubbed;
all wallet addresses were removed from test fixtures and docs.

**Consequence:** `dispatcher.py --once` refuses to start, by design:

```
Config error: wallet.public_key is required. No wallet identity is stored in
this repository ... expose SOLANA_PUBLIC_WALLET (address) and
SOLANA_AGENT_WALLET (keypair) from the OpenClaw secrets store.
```

**What OpenClaw must provide to the service environment:**

| Variable | Contents | Used by |
|---|---|---|
| `SOLANA_PUBLIC_WALLET` | wallet **address** (base58, public) | config_loader, config_gate, preflight, `--check` |
| `SOLANA_AGENT_WALLET` | wallet **keypair** (base58 or JSON byte array) | chain layer (`chain/common.js`) at signing time |
| `SOLANA_RPC_URL` | primary RPC endpoint, key in the query string | every chain call |
| `SOLANA_RPC_FALLBACK_URLS` | optional, comma-separated | re-broadcast of the identical signed tx |
| `SOLANA_WALLET_<ID>` | per-mirror keypair (only if `wallet.mirrors` is used) | mirror legs |

Also unresolved: **which address is current.** Three were in play — one in
`AGENTS.md`/`memory/2026-10-11.md`, one in `agent.config.json`, one in
`archive/` — and all three are now removed. Set the live one in OpenClaw only;
do not write it back into any file in this repo. `test_wallet_hygiene.py` will
fail if one reappears.

**Verify with:** `cd agents/meteora-dlmm/executor && python3 dispatcher.py --check`
It reports `wallet / signer / rpc` separately and lists what is missing. It is
deliberately runnable without a valid config.

**Known gap created by removing the file value:** `config_gate.py` used to refuse
when the live and workspace `agent.config.json` copies named *different* wallets
(the 2026-10-19 incident: reports tracked one wallet while the executor signed
with another). Both copies are empty now, so that check has nothing to compare
and is inert by construction. The equivalent guarantee must come from OpenClaw:
**George, Missy and Sheldon must all see the same `SOLANA_PUBLIC_WALLET`**, or
position scans and exposure figures will describe a different wallet than the one
signing. If you want it enforced in code again, the check to add is
env-vs-scan (`SOLANA_PUBLIC_WALLET` against the wallet id in Missy's newest scan);
say so and it is a small change.

---

## 0. Branch state: the recenter line is parked (resolved 2026-10-30)

`main` (the GitHub default branch) and `master` had diverged on the same base
commit. `main` carried **`3d791ee` — "recenter action, duplicate-open guard,
journal replay, chain rate limiting/retries"** (+575 lines, 2026-10-29), which the
2026-10-29/30 audit never saw; `master` carried the audit and the docs.

Resolution: `main` was fast-forwarded to the audited line and the feature commit
was preserved on its own branch:

| Ref | Points at | Contents |
|---|---|---|
| `main` | audit + docs | **default branch** — audited, documented code |
| `master` | audit + docs | identical to `main` |
| `feature/recenter-2026-10-29` | `3d791ee` | recenter, duplicate-open guard, journal replay, rate limiting |

**Why it was not merged straight in:** the feature commit adds `recenter`,
`Curve` and `BidAsk` to `VALID_ACTIONS`, the builder and the validator — but
`dexes/meteora/chain.js` implements none of them. `Curve`/`BidAsk` are sent to the
chain as literal action names, so before the 2026-10-30 `dispatch.js` fix those
requests returned `{ok: true}` with **no transaction**; today they fail closed.
`Curve`/`BidAsk` also need more than plumbing: the chain open path hardcodes
`StrategyType.Spot`.

**To do when you want it (one task, not a blind merge):**

1. `recenter` — composable to close+open in Python, so it can work; needs its
   close/open legs wired into the refactored dispatch, priority map and
   close-refresh set.
2. `journal replay`, `duplicate-open guard`, `chain rate limiting/retries` — these
   live in `journal.py`, `exposure_guard.py`, `chain_client.py` and merge mostly
   cleanly; re-apply them onto `main`.
3. `Curve` / `BidAsk` — decide: implement (`StrategyType` from the signal) or
   reject at validation. They must not ship as accepted-but-unexecutable actions.

Until then, treat `3d791ee` as a reference implementation, not deployable code.

---

## 2. orca / raydium `add_liquidity` and `remove_liquidity` (OPEN — your call later)
**State:** `validate_core` accepts both actions for **every** DEX, but the Orca
and Raydium chain handlers export no such function. Since the 2026-10-30 change,
`chain/dispatch.js` fails such a request closed ("dex X does not implement action
Y") instead of answering `{ok: true}` with no result — so a signal cannot
silently "succeed" without touching the chain.

**Decision needed:** implement the two actions for Orca/Raydium, or reject them
at validation so a producer cannot emit them.

**Where to look:**
- `executor/dexes/orca/builder.py` and `executor/dexes/raydium/builder.py` (docstrings note it)
- `executor/common/signal_validator.py` → `validate_core` (required fields per action)
- `executor/chain/dispatch.js` → `ACTION_HANDLERS` (the fail-closed table)

Until then: Meteora supports add/remove fully; Orca/Raydium support open, close,
claim (and Raydium's claim is USD-gated by `claim_min_usd`).

---

## 3. JS chain layer has not run against a real SDK (BLOCKED — staging)

**State:** `executor/node_modules` is absent in this checkout, so the chain
handlers cannot be executed here. All JS changes were syntax-checked
(`node --check`) and the pure helpers have `npm test` coverage; the transaction
builders and signers do not.

**Changed in this work:**
- `dexes/raydium/chain.js` — close now reads `tickLowerIndex`/`tickUpperIndex`
  (the fields that exist) and prices min-out from the pool's live
  `sqrtPriceX64`. The old code read `tickLower`/`tickUpper` (undefined) and
  built the slippage floor from `NaN`.
- `chain/dispatch.js` — action dispatch is a table; unimplemented actions now
  fail closed, and a handler returning nothing is an error.
- `chain/amounts.js` — new shared `alignTick` / `withinDepositMax` /
  `deltaWithinBand`; the three DEXes keep their existing tolerances.

**Before the next live wake:** run one simulate-mode open, close and claim per
DEX on staging, then a single small real open+close on the live wallet.

---

## 4. Runtime state is still committed (BLOCKED — your call)

`.gitignore` says "runtime state — never versioned", but these are tracked:

```
agents/meteora-dlmm/signals/failed_verify/*.json   (21 files)
agents/meteora-dlmm/data/dust_swaps/reviewed/*.json (32 files)
agents/meteora-dlmm/ledger/ledger.jsonl
agents/meteora-dlmm/state/{exposure_state,capital_reentry,mirror_sync}.json
```

They are the audit trail, so nothing was deleted. Untracking them is one command
per path (`git rm --cached`), but it lands in history as mass deletions — hence
left to you. `state/circuit_breaker.json` and `state/dispatcher.lock` are
already ignored.

---

## 5. Dust-swap retry holds the dispatcher lock (OPEN)

`dispatcher._handle_swap_to_usdc` sleeps **60 s** between its two attempts while
holding the global dispatcher lock. Closes are processed first and are never
deferred, so the exposure is a delayed (not skipped) claim/open in the same
wake — but a close arriving during that minute waits.

The retry policy is untested, so changing it is a behaviour decision. My
recommendation: keep one attempt per wake (the code comment already says the
"normal queue rails own the next attempt") and drop the inline sleep. Say the
word and it is a small change.

---

## 6. Remaining consolidation (OPEN — hygiene, no behaviour change)

Deferred because they are safety-adjacent and deserve their own review pass:

- **4f** `_run_wallet_refresh` / `_run_position_refresh` share ~80% (4 attempts,
  subprocess, timeout, "scan not refreshed → sleep 6 → retry").
- **4g** the three exposure-guard checks (`check_open_allowed`,
  `check_add_allowed`, `_check_replace_position`) re-implement the same
  sizing + daily-loss + drawdown trio with slightly different wording.
- **Step 5** single JSON read per pending signal (currently up to 4 reads), and
  one wallet-scan read per wake instead of two.
- **Test debt** 25 files repeat `sys.path.insert`; 6 files carry their own
  `RAILS` fixture written against different rail eras (300 vs 500 exposure,
  50 vs 10 daily loss).

---

## 7. Rails that exist but are not enforced (awareness)

`execution_limits.json` / `SAFETY_RAILS.md` document these; the executor reads
none of them:

| Rail | Status |
|---|---|
| `pause_after_consecutive_failures` | **implemented 2026-10-30** (circuit breaker) |
| `pause_on_rpc_error_streak` | **implemented 2026-10-30** — but note it is a subset of the failure rail: with the shipped 3 / 5 values the failure rail always trips first. It only changes behaviour if set below the failure cap. |
| `open_window_utc`, `close_window_utc`, `blackout_dates` | not enforced (producer-side scheduling hints) |
| `one_tx_at_a_time` | not enforced (serialization is the dispatcher lock, which is stronger) |
| `tx_timeout_seconds` | not read by the chain layer (timeouts are hardcoded: 120 s subprocess, Node defaults) |
| `min_pool_liquidity_usd`, `min_24h_volume_usd` | loaded, not enforced by George (Sheldon screens pools) |

Each is now commented in `common/rails_loader.py` so it no longer *looks* live.

---

## 8. Resuming after a self-pause (operating note)

When three consecutive executions fail the executor pauses itself and stops
processing signals (`exit 76` from `--once`):

1. read the alert: `SELF-PAUSE <n> consecutive execution failures ... signal=... stage=... error=...`
2. check the journal for that signal (`journal/<date>.jsonl`) and the chain state;
3. fix the cause (RPC, funds, route, pool);
4. `cd agents/meteora-dlmm/executor && python3 dispatcher.py --resume`.

Nothing resumes automatically, and `--resume` does not raise any rail: the same
threshold trips again on the next three failures. Lifetime failure count is kept
in `state/circuit_breaker.json` for observability.
