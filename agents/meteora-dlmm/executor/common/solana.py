"""Solana mints and program ids used by the executor.

These literals were previously repeated in dispatcher.py, live_preflight.py,
ledger.py and (in the JS layer) prices.js. A wrong mint or a wrong program id
is not a cosmetic error — it silently changes which token is read and which
account layout is trusted — so they get one definition each.

Nothing here is a secret; all values are public mainnet identifiers.
"""

# Quote asset for the executor and the dust-sweep target.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Wrapped SOL. Native SOL and WSOL are the same asset for the executor's
# purposes: a WSOL side of a position is funded from the wallet's native
# balance, and a USDC->WSOL swap is both the gas top-up and the SOL side.
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Alias kept because the trade ledger names this asset "SOL" in its rows.
SOL_MINT = WSOL_MINT

# SPL token programs. Balances must be read from both: a mint may live in the
# Token-2022 program, and missing it would understate a holding to zero.
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Owning program of each pool account. Reading a pool's bytes without checking
# the owner can decode a plausible tick/bin-step out of an unrelated account,
# so every pool read is owner-checked against this table.
DEX_PROGRAM_IDS = {
    "meteora": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    "orca": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "raydium": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
}
