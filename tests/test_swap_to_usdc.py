"""Tests for Sheldon's swap_to_usdc signal handling in George."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# Make the executor modules importable from this workspace-level test.
_EXECUTOR_DIR = Path(__file__).resolve().parent.parent / "agents" / "meteora-dlmm" / "executor"
sys.path.insert(0, str(_EXECUTOR_DIR))

import common.dust_queue as dust_queue
import common.signal_validator as signal_validator
from dexes.meteora import validator as meteora_validator
from dexes.raydium import validator as raydium_validator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_swap_to_usdc(**overrides) -> dict:
    signal = {
        "signal_id": "test-swap-to-usdc-001",
        "action": "swap_to_usdc",
        "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "symbol": "dust",
        "decimals": 6,
        "amount_raw": "1000000",
        "amount_ui": 1.0,
        "value_usd": 0.42,
        "reason": "Dust non-SOL/non-USDC asset value $0.42 > $0.10 threshold",
        "created_at": _now_iso(),
    }
    signal.update(overrides)
    return signal


def _valid_open(action: str = "open", dex: str = "raydium", width: int = 2000) -> dict:
    # Use inclusive bin count: upper = lower + width - 1, so a width of 2000
    # spans exactly 2000 bins/ticks and is at the rail cap.
    return {
        "signal_id": f"test-{action}-{dex}-{width}",
        "action": action,
        "dex": dex,
        "pool_address": "So1anaPoo1AddresS123456789012345678901234567890",
        "bin_range": {"lower": 0, "upper": width - 1},
        "liquidity": {"amount_x": "1000000", "amount_y": "500000"},
        "max_slippage_bps": 50,
        "reason": "test range width",
        "created_at": _now_iso(),
    }


class TestSwapToUsdc(unittest.TestCase):
    """Validate and queue swap_to_usdc signals from Sheldon."""

    def setUp(self):
        self._original_data_dir = dust_queue.DATA_DIR
        self._original_pending_dir = dust_queue.PENDING_DIR
        self._original_reviewed_dir = dust_queue.REVIEWED_DIR
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        dust_queue.DATA_DIR = tmp_path
        dust_queue.PENDING_DIR = tmp_path / "pending"
        dust_queue.REVIEWED_DIR = tmp_path / "reviewed"

    def tearDown(self):
        dust_queue.DATA_DIR = self._original_data_dir
        dust_queue.PENDING_DIR = self._original_pending_dir
        dust_queue.REVIEWED_DIR = self._original_reviewed_dir
        self.tmpdir.cleanup()

    def test_valid_swap_to_usdc_parsed_and_queued(self):
        """A valid swap_to_usdc signal passes validation and can be queued."""
        signal = _valid_swap_to_usdc()
        cfg = {"signal_max_age_seconds": 300}
        rails = {"max_bin_range_width": 2000}

        validated = signal_validator.validate_core(signal, cfg, rails)
        self.assertEqual(validated["action"], "swap_to_usdc")
        self.assertEqual(validated["symbol"], "dust")

        path = dust_queue.queue(validated)
        self.assertTrue(path.exists())

        pending = dust_queue.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending_review")
        self.assertEqual(pending[0]["signal_id"], signal["signal_id"])

        loaded = dust_queue.get(signal["signal_id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["signal"]["mint"], signal["mint"])

    def test_missing_and_invalid_fields_rejected(self):
        """swap_to_usdc signals with missing or malformed fields are rejected."""
        cfg = {"signal_max_age_seconds": 300}
        rails = {"max_bin_range_width": 2000}

        missing_cases = {
            "missing mint": _valid_swap_to_usdc(mint=None),
            "missing symbol": _valid_swap_to_usdc(symbol=""),
            "missing amount_raw": _valid_swap_to_usdc(amount_raw=""),
            "negative value_usd": _valid_swap_to_usdc(value_usd=-1.0),
            "bad decimals": _valid_swap_to_usdc(decimals="abc"),
            "short mint": _valid_swap_to_usdc(mint="short"),
        }

        for name, signal in missing_cases.items():
            with self.subTest(name=name):
                with self.assertRaises(signal_validator.SignalValidationError):
                    signal_validator.validate_core(signal, cfg, rails)

    def test_range_width_guardrail_respects_new_maximum(self):
        """Range width must not exceed the rail (2000 by default)."""
        cfg = {"signal_max_age_seconds": 300}
        rails = {"max_bin_range_width": 2000}

        # Exactly at the new cap should pass.
        raydium_validator.validate(_valid_open(dex="raydium", width=2000), cfg, rails)

        # One bin over should fail.
        with self.assertRaises(signal_validator.SignalValidationError) as ctx:
            raydium_validator.validate(_valid_open(dex="raydium", width=2001), cfg, rails)
        self.assertIn("exceeds rail", str(ctx.exception))

    def test_dex_specific_range_width(self):
        """Per-DEX range-width overrides take precedence over the shared cap."""
        cfg = {"signal_max_age_seconds": 300}
        rails = {
            "max_bin_range_width": 2000,
            "max_raydium_range_width": 500,
        }

        # Raydium width 600 exceeds its DEX-specific cap.
        with self.assertRaises(signal_validator.SignalValidationError) as ctx:
            raydium_validator.validate(_valid_open(dex="raydium", width=600), cfg, rails)
        self.assertIn("raydium range width 600 (inclusive) exceeds rail 500", str(ctx.exception))

        # Meteora is capped at 70 bins by default. The bin-step rail is
        # patched to an allowed step: width is what this test exercises.
        with patch("common.signal_validator.fetch_pool_bin_step", return_value=10):
            meteora_validator.validate(_valid_open(dex="meteora", width=70), cfg, rails)
        with patch("common.signal_validator.fetch_pool_bin_step", return_value=10), \
                self.assertRaises(signal_validator.SignalValidationError) as ctx:
            meteora_validator.validate(_valid_open(dex="meteora", width=71), cfg, rails)
        self.assertIn("meteora range width 71 (inclusive) exceeds rail 70", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
