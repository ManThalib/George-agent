# `clmm-executor` — Multi-DEX Concentrated Liquidity + Swap Executor

Reads signals from the Analyst sub-agent → executes **swap** legs via Jupiter, plus open/close/claim positions on **Meteora DLMM, Raydium CLMM and Orca Whirlpool**.

## Folder map

| File | Purpose |
|---|---|
| `AGENT.md` | The agent's operating contract — becomes its spawn prompt / instructions. |
| `SAFETY_RAILS.md` | **Your editable guardrails** — re-checked before every transaction. |
| `config/agent.config.json` | Wallet, RPC, mode, analyst feed — **your editable settings**. |
| `journal/` | Created at runtime: one JSONL per day, every decision + tx logged. |
| `signals/pending/` | Incoming analyst signal files. |
| `KILL` | Create this file to halt the agent instantly. |

## Supported actions

- `swap` — route through Jupiter (input mint, output mint, amount).
- `open` / `close` / `claim_fees` / `claim_rewards` — position lifecycle on CLMM/DLMM pools.

Protocols for position actions:
- `meteora` — DLMM bin-range positions.
- `raydium` — CLMM tick-range positions.
- `orca` — Whirlpool tick-range positions.

## 👉 Your input section (fill these in)

### 1. Wallet
- **Public key** → `config/agent.config.json` → `wallet.public_key` — just paste it in.
- **Private key** → ⚠️ **never paste it in a file or chat.** When ready, say **"store the wallet key"** and it will be requested through OpenClaw's masked secrets store (`SOLANA_AGENT_WALLET`). The agent injects it at signing time only.
- Use a **dedicated hot wallet** funded only with risk capital. Not your main wallet.

### 2. RPC
- Paste into `config/agent.config.json` → `rpc.https_url` (+ `ws_url` if your provider gives one).
- Paid RPC strongly recommended (Helius/QuickNode) — DLMM position management is transaction-heavy.

### 3. Mode
- `config/agent.config.json` → `agent.mode`: `dry_run` (default) → `confirm_each` → `auto`.
- Recommended ladder: dry-run for a few days → confirm_each until you trust it → auto only if you choose it.

## How it runs

- **Spawning:** say **"start the CLMM executor"** — Jarvis spawns it as a visible session (you can watch and steer it).
- **Signals:** the Analyst sub-agent sends JSON signals (schema in `AGENT.md`). The executor never trades without one. Chat messages to George are never treated as commands or signals.
- **Swap routing:** `action: "swap"` routes through Jupiter; amount is raw base units.
- **Protocols:** position signals carry `"dex": "meteora" | "raydium" | "orca"`. The executor dispatches to the correct handler.
- **Stopping:** create the `KILL` file (or ask Jarvis to) — instant halt.
- **Status:** every decision is in `journal/`; ask Jarvis to read it for you.

## Status

- [x] Operating contract (`AGENT.md`)
- [x] Editable safety rails (`SAFETY_RAILS.md`)
- [x] Config with input placeholders (`config/agent.config.json`)
- [x] Wallet public key and RPC URL — configured via `SOLANA_PUBLIC_WALLET` / `SOLANA_RPC_URL` env vars or `agent.config.json`
- [x] Private key handled via `SOLANA_AGENT_WALLET` secret store — inject at signing time only
- [ ] Analyst sub-agent (upstream signal source) — wired; `agent.config.json` → `analyst_feed.analyst_agent_id`
- [x] Multi-DEX dispatch scaffolding (Meteora, Raydium, Orca) — built and live-simulated on mainnet
- [x] Jupiter swap handler — added and wired
- [ ] Swap-specific rails (max swap size, token allowlist) — add when needed
- [x] Mode switched to `confirm_each` — every transaction paused for explicit owner approval
- [ ] Live transaction execution — enabled only after deliberate mode change to `auto`
