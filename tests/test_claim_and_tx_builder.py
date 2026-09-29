"""Tests for claim signal handling and tx_builder routing."""

import sys
from datetime import datetime, timezone
import unittest
from pathlib import Path

_EXECUTOR_DIR = Path(__file__).resolve().parent.parent / "agents" / "meteora-dlmm" / "executor"
sys.path.insert(0, str(_EXECUTOR_DIR))

import tx_builder


class TestTxBuilderRouting(unittest.TestCase):
    """Chain-action routing from George signal to chain CLI request."""

    def _req(self, signal):
        cfg = {
            "rpc_https_url": "https://rpc.test",
            "wallet_public_key": "QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj",
        }
        rails = {"max_slippage_bps": 100}
        return tx_builder._request_from_signal("send", signal, cfg, rails)

    def test_swap_routes_to_jupiter(self):
        signal = {
            "signal_id": "test-swap-001",
            "action": "swap",
            "input_mint": "So11111111111111111111111111111111111111112",
            "output_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "amount": "1000000",
        }
        req = self._req(signal)
        self.assertEqual(req["dex"], "jupiter")
        self.assertEqual(req["action"], "swap")

    def test_open_routes_to_open(self):
        signal = {
            "signal_id": "test-open-001",
            "action": "open",
            "dex": "meteora",
            "pool_address": "Pool111111111111111111111111111111111111111",
            "bin_range": {"lower": 0, "upper": 10},
            "liquidity": {"amount_x": "1000000", "amount_y": "500000"},
        }
        req = self._req(signal)
        self.assertEqual(req["action"], "open")

    def test_claim_fees_routes_to_claim(self):
        signal = {
            "signal_id": "test-claim-fees-001",
            "action": "claim_fees",
            "dex": "orca",
            "pool_address": "Pool111111111111111111111111111111111111111",
            "position_id": "Pos1111111111111111111111111111111111111111",
        }
        req = self._req(signal)
        self.assertEqual(req["action"], "claim")

    def test_claim_rewards_routes_to_claim(self):
        signal = {
            "signal_id": "test-claim-rewards-001",
            "action": "claim_rewards",
            "dex": "raydium",
            "pool_address": "Pool111111111111111111111111111111111111111",
            "position_id": "Pos1111111111111111111111111111111111111111",
        }
        req = self._req(signal)
        self.assertEqual(req["action"], "claim")


class TestSignalValidatorClaim(unittest.TestCase):
    """Claim signals should not require bin_range or liquidity."""

    def _validate(self, signal):
        from signal_validator import validate_dict
        cfg = {"signal_max_age_seconds": 300}
        rails = {"max_bin_range_width": 2000}
        return validate_dict(signal, cfg, rails)

    def test_claim_fees_passes_without_bin_range_or_liquidity(self):
        signal = {
            "signal_id": "test-claim-fees-002",
            "action": "claim_fees",
            "dex": "orca",
            "pool_address": "Pool111111111111111111111111111111111111111",
            "position_id": "Pos1111111111111111111111111111111111111111",
            "max_slippage_bps": 50,
            "created_at": "2026-09-29T06:55:00+00:00",
            "reason": "test",
        }
        signal["created_at"] = datetime.now(timezone.utc).isoformat()
        validated = self._validate(signal)
        self.assertEqual(validated["action"], "claim_fees")

    def test_claim_rewards_passes_without_bin_range_or_liquidity(self):
        signal = {
            "signal_id": "test-claim-rewards-002",
            "action": "claim_rewards",
            "dex": "raydium",
            "pool_address": "Pool111111111111111111111111111111111111111",
            "position_id": "Pos1111111111111111111111111111111111111111",
            "max_slippage_bps": 50,
            "created_at": "2026-09-29T06:55:00+00:00",
            "reason": "test",
        }
        signal["created_at"] = datetime.now(timezone.utc).isoformat()
        validated = self._validate(signal)
        self.assertEqual(validated["action"], "claim_rewards")


if __name__ == "__main__":
    unittest.main()
