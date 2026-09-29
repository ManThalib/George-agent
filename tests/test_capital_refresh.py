"""Tests for George's post-swap capital refresh chain (swap -> wallet rescan
-> Sheldon re-entry) and the Sheldon prep-swap signal schema."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_EXECUTOR_DIR = Path(__file__).resolve().parent.parent / "agents" / "meteora-dlmm" / "executor"
sys.path.insert(0, str(_EXECUTOR_DIR))

import dispatcher
from common import signal_validator


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class ReentryGuardTests(unittest.TestCase):
    """Hourly loop guard for swap -> refresh -> re-entry chains."""

    def test_empty_state_allows(self):
        allowed, reason = dispatcher._reentry_allowed({"timestamps": []}, now=1000.0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_missing_state_allows(self):
        allowed, _ = dispatcher._reentry_allowed({}, now=1000.0)
        self.assertTrue(allowed)

    def test_eight_recent_entries_block(self):
        state = {"timestamps": [1000.0 - i * 10 for i in range(8)]}
        allowed, reason = dispatcher._reentry_allowed(state, now=1000.0)
        self.assertFalse(allowed)
        self.assertIn("loop guard", reason)

    def test_old_entries_expire(self):
        state = {"timestamps": [1000.0 - 3601 - i for i in range(8)]}
        allowed, _ = dispatcher._reentry_allowed(state, now=1000.0)
        self.assertTrue(allowed)

    def test_mixed_old_and_recent_counts_recent_only(self):
        state = {"timestamps": [1000.0 - i * 10 for i in range(7)] + [1000.0 - 4000]}
        allowed, _ = dispatcher._reentry_allowed(state, now=1000.0)
        self.assertTrue(allowed)

    def test_non_numeric_timestamps_ignored(self):
        state = {"timestamps": ["bad", None, 1000.0 - 10]}
        allowed, _ = dispatcher._reentry_allowed(state, now=1000.0)
        self.assertTrue(allowed)


class PrepSwapSignalTests(unittest.TestCase):
    """Sheldon prep swaps must validate through George's core validator."""

    def _cfg(self):
        return {
            "rpc_https_url": "https://rpc.test",
            "wallet_public_key": "QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj",
        }

    def test_buy_prep_swap_validates(self):
        signal = {
            "signal_id": "sheldon-prep-123-1",
            "action": "swap",
            "input_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "output_mint": "So11111111111111111111111111111111111111112",
            "amount": "15000000",
            "max_slippage_bps": 100,
            "direction": "buy",
            "prep_for_pool": "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6",
            "reason": "CAPITAL PREP buy SOL",
            "created_at": _hours_ago_iso(0),
        }
        validated = signal_validator.validate_core(signal, self._cfg(), {"max_slippage_bps": 100})
        self.assertEqual(validated["action"], "swap")
        self.assertEqual(validated["amount"], "15000000")

    def test_prep_swap_over_rail_slippage_rejected(self):
        signal = {
            "signal_id": "sheldon-prep-123-2",
            "action": "swap",
            "input_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "output_mint": "So11111111111111111111111111111111111111112",
            "amount": "15000000",
            "max_slippage_bps": 500,
            "reason": "CAPITAL PREP buy SOL",
            "created_at": _hours_ago_iso(0),
        }
        with self.assertRaises(signal_validator.SignalValidationError):
            signal_validator.validate_core(signal, self._cfg(), {"max_slippage_bps": 100})


if __name__ == "__main__":
    unittest.main(verbosity=2)
