#!/usr/bin/env python3
"""No wallet identity lives in this repository.

Owner instruction 2026-10-30: the wallet address and keypair are a secret
reference in the OpenClaw secrets store, reached as SOLANA_PUBLIC_WALLET and
SOLANA_AGENT_WALLET. Every previously committed address was removed — from the
live config, the archived rotation config, the test fixtures and the docs.

These tests fail if one is reintroduced, which is the realistic regression:
someone pastes an address into agent.config.json to "make it run locally".
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import paths  # noqa: E402

# A base58-ish run long enough to be a Solana address (32-44 chars). Well-known
# program/mint ids are excluded: they are protocol constants, not identities.
BASE58_ADDRESS = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
PUBLIC_CONSTANTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "So11111111111111111111111111111111111111112",    # WSOL
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",    # SPL token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",    # Token-2022
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",    # Meteora DLMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",    # Orca Whirlpool
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",   # Raydium CLMM
}


class NoWalletInConfigTests(unittest.TestCase):
    def test_live_config_has_no_wallet_value(self):
        cfg = json.loads(paths.config_path().read_text(encoding="utf-8"))
        self.assertEqual(cfg["wallet"]["public_key"], "")
        self.assertIn("_public_key_note", cfg["wallet"])

    def test_archived_rotation_config_has_no_wallet_value(self):
        archive = paths.agent_root() / "archive" / "wallet-rotation-previous-config.json"
        if not archive.exists():
            self.skipTest("archive not present")
        cfg = json.loads(archive.read_text(encoding="utf-8"))
        self.assertEqual(cfg["wallet"]["public_key"], "")

    def test_mirror_entries_carry_no_keys(self):
        cfg = json.loads(paths.config_path().read_text(encoding="utf-8"))
        for mirror in cfg.get("wallet", {}).get("mirrors") or []:
            self.assertEqual(str(mirror.get("public_key") or ""), "")


class NoWalletInFilesTests(unittest.TestCase):
    """Sweep the shipped config surface for a bare address."""

    def test_no_address_shaped_value_in_shipped_config(self):
        root = paths.agent_root()
        candidates = [root / "execution_limits.json"]
        for sub in ("config", "archive"):
            candidates += sorted((root / sub).glob("*.json"))
        offenders = []
        for path in candidates:
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in BASE58_ADDRESS.findall(text):
                if match not in PUBLIC_CONSTANTS:
                    offenders.append(f"{path.name}: {match[:6]}…")
        self.assertEqual(offenders, [],
                         "wallet-shaped address in a shipped config file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
