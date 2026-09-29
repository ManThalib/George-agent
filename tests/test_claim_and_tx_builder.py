"""Tests for claim signal handling and request routing.

Updated for the per-DEX restructure: swap requests are built by
``dispatcher._build_swap_request`` (Jupiter), position requests by
``dexes/<dex>/builder.build``, and validation lives in
``common.signal_validator.validate_core``.
"""

import sys
from datetime import datetime, timezone
import unittest
from pathlib import Path

_EXECUTOR_DIR = Path(__file__).resolve().parent.parent / "agents" / "meteora-dlmm" / "executor"
sys.path.insert(0, str(_EXECUTOR_DIR))

import dispatcher
from common import signal_validator
from dexes.meteora import builder as meteora_builder
from dexes.orca import builder as orca_builder
from dexes.raydium import builder as raydium_builder


class TestRequestRouting(unittest.TestCase):
    """Chain-action routing from George signal to chain CLI request."""

    CFG = {
        "rpc_https_url": "https://rpc.test",
        "wallet_public_key": "QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj",
    }
    RAILS = {"max_slippage_bps": 100}

    def test_swap_routes_to_jupiter(self):
        signal = {
            "signal_id": "test-swap-001",
            "action": "swap",
            "input_mint": "So11111111111111111111111111111111111111112",
            "output_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "amount": "1000000",
        }
        req = dispatcher._build_swap_request(signal, self.CFG, self.RAILS)
        self.assertEqual(req["dex"], "jupiter")
        self.assertEqual(req["action"], "swap")
        self.assertEqual(req["amount"], "1000000")

    def test_open_routes_to_open(self):
        signal = {
            "signal_id": "test-open-001",
            "action": "open",
            "dex": "meteora",
            "pool_address": "Pool111111111111111111111111111111111111111",
            "bin_range": {"lower": 0, "upper": 10},
            "liquidity": {"amount_x": "1000000", "amount_y": "500000"},
        }
        req = meteora_builder.build(signal, self.CFG, self.RAILS)
        self.assertEqual(req["action"], "open")
        self.assertEqual(req["dex"], "meteora")
        self.assertEqual(req["bin_range"], {"lower": 0, "upper": 10})

    def test_claim_fees_routes_to_claim(self):
        signal = {
            "signal_id": "test-claim-fees-001",
            "action": "claim_fees",
            "dex": "orca",
            "pool_address": "Pool111111111111111111111111111111111111111",
            "position_id": "Pos1111111111111111111111111111111111111111",
        }
        req = orca_builder.build(signal, self.CFG, self.RAILS)
        self.assertEqual(req["action"], "claim")

    def test_claim_rewards_routes_to_claim(self):
        signal = {
            "signal_id": "test-claim-rewards-001",
            "action": "claim_rewards",
            "dex": "raydium",
            "pool_address": "Pool111111111111111111111111111111111111111",
            "position_id": "Pos1111111111111111111111111111111111111111",
        }
        req = raydium_builder.build(signal, self.CFG, self.RAILS)
        self.assertEqual(req["action"], "claim")


class TestSignalValidatorClaim(unittest.TestCase):
    """Claim signals should not require bin_range or liquidity."""

    def _validate(self, signal):
        cfg = {"signal_max_age_seconds": 300}
        rails = {"max_bin_range_width": 2000}
        return signal_validator.validate_core(signal, cfg, rails)

    def _signal(self, action, dex):
        return {
            "signal_id": f"test-{action}-002",
            "action": action,
            "dex": dex,
            "pool_address": "Pool111111111111111111111111111111111111111",
            "position_id": "Pos1111111111111111111111111111111111111111",
            "max_slippage_bps": 50,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "test",
        }

    def test_claim_fees_passes_without_bin_range_or_liquidity(self):
        validated = self._validate(self._signal("claim_fees", "orca"))
        self.assertEqual(validated["action"], "claim_fees")

    def test_claim_rewards_passes_without_bin_range_or_liquidity(self):
        validated = self._validate(self._signal("claim_rewards", "raydium"))
        self.assertEqual(validated["action"], "claim_rewards")


if __name__ == "__main__":
    unittest.main()
