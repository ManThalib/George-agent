"""Tests for the add_liquidity / remove_liquidity signal actions (2026-10-01).

Covers: shared validator rules, meteora per-DEX rails, builder request
mapping, exposure-guard math, and the min_remove_bps rail load.

Hermetic: live RPC fetches are mocked.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

_EXECUTOR_DIR = Path(__file__).resolve().parent.parent / "agents" / "meteora-dlmm" / "executor"
sys.path.insert(0, str(_EXECUTOR_DIR))

import common.exposure_guard as exposure_guard
import common.signal_validator as signal_validator
from common.rails_loader import load_rails
from common.signal_validator import SignalValidationError, validate_core
from dexes.meteora import builder as meteora_builder
from dexes.meteora import validator as meteora_validator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_add(**overrides) -> dict:
    signal = {
        "signal_id": "test-add-001",
        "action": "add_liquidity",
        "dex": "meteora",
        "pool_address": "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y",
        "position_id": "43ivjtQ7s8AweC2suoAULYAhPVtQRapAfsahdXDDs1QM",
        "bin_range": {"lower": -2156, "upper": -2108},
        "liquidity": {"amount_x": "1220993", "amount_y": "144793"},
        "position_usd": 10.0,
        "created_at": _now_iso(),
        "max_slippage_bps": 100,
    }
    signal.update(overrides)
    return signal


def _valid_remove(**overrides) -> dict:
    signal = {
        "signal_id": "test-remove-001",
        "action": "remove_liquidity",
        "dex": "meteora",
        "pool_address": "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y",
        "position_id": "43ivjtQ7s8AweC2suoAULYAhPVtQRapAfsahdXDDs1QM",
        "bps": 5000,
        "created_at": _now_iso(),
        "max_slippage_bps": 100,
    }
    signal.update(overrides)
    return signal


RAILS = {
    "max_slippage_bps": 100,
    "min_position_usd": 10.0,
    "max_position_usd": 100.0,
    "min_open_score": 70.0,
    "max_meteora_range_width": 70,
    "min_remove_bps": 1,
}
CFG = {
    "rpc_https_url": "https://rpc.test",
    "signal_max_age_seconds": 300,
    "wallet_public_key": "QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj",
}


class TestAddRemoveValidator(unittest.TestCase):
    def test_add_valid(self):
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            out = validate_core(_valid_add(), CFG, RAILS)
            out = meteora_validator.validate(out, CFG, RAILS)
        self.assertEqual(out["action"], "add_liquidity")

    def test_add_missing_position_usd(self):
        bad = _valid_add()
        del bad["position_usd"]
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)

    def test_add_zero_liquidity_rejected(self):
        bad = _valid_add(liquidity={"amount_x": "0", "amount_y": "0"})
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)

    def test_add_negative_amount_rejected(self):
        bad = _valid_add(liquidity={"amount_x": "-5", "amount_y": "0"})
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)

    def test_add_tick_out_of_range_rejected(self):
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-1900):
            with self.assertRaises(SignalValidationError):
                validate_core(_valid_add(), CFG, RAILS)

    def test_add_width_exceeds_meteora_rail(self):
        bad = _valid_add(bin_range={"lower": -3000, "upper": -2000})
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            signal = validate_core(bad, CFG, RAILS)
            with self.assertRaises(SignalValidationError):
                meteora_validator.validate(signal, CFG, RAILS)

    def test_remove_valid(self):
        out = validate_core(_valid_remove(), CFG, RAILS)
        out = meteora_validator.validate(out, CFG, RAILS)
        self.assertEqual(out["action"], "remove_liquidity")

    def test_remove_bps_10000_rejected_full_removal_is_close(self):
        bad = _valid_remove(bps=10000)
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)

    def test_remove_bps_zero_rejected(self):
        bad = _valid_remove(bps=0)
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)

    def test_remove_non_integer_bps_rejected(self):
        bad = _valid_remove(bps="half")
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)

    def test_remove_missing_position_id_rejected(self):
        bad = _valid_remove()
        del bad["position_id"]
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)


class TestMeteoraBuilder(unittest.TestCase):
    def test_add_request_mapping(self):
        req = meteora_builder.build(_valid_add(), CFG, RAILS)
        self.assertEqual(req["action"], "add_liquidity")
        self.assertEqual(req["position_id"], "43ivjtQ7s8AweC2suoAULYAhPVtQRapAfsahdXDDs1QM")
        self.assertEqual(req["bin_range"], {"lower": -2156, "upper": -2108})
        self.assertEqual(req["liquidity"], {"amount_x": "1220993", "amount_y": "144793"})

    def test_remove_request_mapping(self):
        req = meteora_builder.build(_valid_remove(claim_fees=True), CFG, RAILS)
        self.assertEqual(req["action"], "remove_liquidity")
        self.assertEqual(req["bps"], 5000)
        self.assertTrue(req["claim_after"])
        self.assertNotIn("liquidity", req)
        self.assertNotIn("bin_range", req)


class TestExposureGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        state_path = Path(self._tmp.name) / "exposure_state.json"
        state_path.write_text(
            '{"open_positions": [{"pool_address": "POOL", "position_usd": 50.0,'
            ' "opened_at": "2026-10-01T00:00:00+00:00"}],'
            ' "daily_loss_usd": 0.0, "max_daily_loss_usd": 0.0,'
            ' "peak_portfolio_usd": 0.0, "session_start_value_usd": 0.0,'
            ' "last_reset_date": "2026-10-01"}'
        )
        patcher = patch.object(exposure_guard, "STATE_PATH", state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _add_signal(self, **kw):
        base = {
            "signal_id": "t-add",
            "action": "add_liquidity",
            "pool_address": "POOL",
            "position_id": "POS1",
            "position_usd": 30.0,
            "bps": 0,
        }
        base.update(kw)
        return base

    def test_add_allowed_within_caps(self):
        allowed, reason = exposure_guard.check_add_allowed(self._add_signal(), RAILS)
        self.assertTrue(allowed, reason)

    def test_add_rejected_over_per_position_cap(self):
        allowed, reason = exposure_guard.check_add_allowed(
            self._add_signal(position_usd=60.0), RAILS
        )
        self.assertFalse(allowed)
        self.assertIn("max_position_usd", reason)

    def test_add_untracked_position_falls_back_to_add_alone(self):
        signal = self._add_signal(pool_address="OTHER", position_usd=50.0)
        allowed, reason = exposure_guard.check_add_allowed(signal, RAILS)
        self.assertTrue(allowed, reason)
        allowed, reason = exposure_guard.check_add_allowed(
            {**signal, "position_usd": 150.0}, RAILS
        )
        self.assertFalse(allowed)

    def test_record_add_grows_and_backfills_position_id(self):
        exposure_guard.record_add_executed(self._add_signal())
        state = exposure_guard.load_state()
        entry = state["open_positions"][0]
        self.assertAlmostEqual(entry["position_usd"], 80.0)
        self.assertEqual(entry["position_id"], "POS1")

    def test_record_remove_shrinks_proportionally(self):
        exposure_guard.record_add_executed(self._add_signal())  # 50 + 30 = 80
        exposure_guard.record_remove_executed(
            {"pool_address": "POOL", "position_id": "POS1", "bps": 5000}
        )
        entry = exposure_guard.load_state()["open_positions"][0]
        self.assertAlmostEqual(entry["position_usd"], 40.0)
        # Entry survives a partial remove: the position is still open on-chain.
        self.assertEqual(len(exposure_guard.load_state()["open_positions"]), 1)


class TestRailLoad(unittest.TestCase):
    def test_min_remove_bps_rail_loads(self):
        # grep alone cannot detect a silent parse failure; assert the loader.
        self.assertEqual(int(load_rails().get("min_remove_bps", 0)), 1)

    def test_min_open_score_loads_from_scoring_block(self):
        # Sheldon Phase 2 moved min_open_score from pool_eligibility to the
        # scoring block; the loader must follow or the executor silently
        # falls back to DEFAULT_RAILS and drifts from the producer.
        rails = load_rails()
        self.assertEqual(float(rails.get("min_open_score", 0.0)), 70.0)
        self.assertEqual(rails.get("scoring_source"), "missy")


if __name__ == "__main__":
    unittest.main()
