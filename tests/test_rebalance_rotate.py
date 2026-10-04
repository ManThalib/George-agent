"""Tests for rebalance / rotate signal actions and mutation cooldowns."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

_EXECUTOR_DIR = Path(__file__).resolve().parent.parent / "agents" / "meteora-dlmm" / "executor"
sys.path.insert(0, str(_EXECUTOR_DIR))

import common.exposure_guard as exposure_guard
import common.signal_validator as signal_validator
from common.signal_validator import SignalValidationError, validate_core
from dexes.meteora import builder as meteora_builder
from dexes.meteora import validator as meteora_validator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


RAILS = {
    "max_slippage_bps": 100,
    "min_position_usd": 10.0,
    "max_position_usd": 100.0,
    "min_open_score": 70.0,
    "min_open_score_adaptive_floor": 55.0,
    "max_meteora_range_width": 70,
    "min_remove_bps": 1,
    "partial_remove_bps_min": 2500,
    "partial_remove_bps_max": 7500,
    "mutation_cooldown_hours": 6,
    "max_total_exposure_usd": 300.0,
    "max_daily_loss_usd": 50.0,
    "max_drawdown_pct": 10.0,
}

CFG = {
    "rpc_https_url": "https://rpc.test",
    "signal_max_age_seconds": 300,
    "wallet_public_key": "QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj",
}


def _valid_rebalance(**overrides) -> dict:
    signal = {
        "signal_id": "test-rebalance-001",
        "action": "rebalance",
        "dex": "meteora",
        "pool_address": "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y",
        "position_id": "43ivjtQ7s8AweC2suoAULYAhPVtQRapAfsahdXDDs1QM",
        "bin_range": {"lower": -2156, "upper": -2108},
        "liquidity": {"amount_x": "1220993", "amount_y": "144793"},
        "position_usd": 50.0,
        "side": "bidirectional",
        "created_at": _now_iso(),
        "max_slippage_bps": 100,
    }
    signal.update(overrides)
    return signal


def _valid_rotate(**overrides) -> dict:
    signal = {
        "signal_id": "test-rotate-001",
        "action": "rotate",
        "dex": "meteora",
        "pool_address": "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y",
        "position_id": "43ivjtQ7s8AweC2suoAULYAhPVtQRapAfsahdXDDs1QM",
        "bin_range": {"lower": -2156, "upper": -2108},
        "liquidity": {"amount_x": "1220993", "amount_y": "144793"},
        "position_usd": 50.0,
        "side": "bidirectional",
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


class TestRebalanceRotateValidator(unittest.TestCase):
    def test_rebalance_valid(self):
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            out = validate_core(_valid_rebalance(), CFG, RAILS)
            out = meteora_validator.validate(out, CFG, RAILS)
        self.assertEqual(out["action"], "rebalance")

    def test_rotate_valid(self):
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            out = validate_core(_valid_rotate(), CFG, RAILS)
            out = meteora_validator.validate(out, CFG, RAILS)
        self.assertEqual(out["action"], "rotate")

    def test_rebalance_missing_position_id_rejected(self):
        bad = _valid_rebalance()
        del bad["position_id"]
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)

    def test_rotate_missing_bin_range_rejected(self):
        bad = _valid_rotate()
        del bad["bin_range"]
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)

    def test_rebalance_range_width_rail_applies(self):
        bad = _valid_rebalance(bin_range={"lower": -3000, "upper": -2000})
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            signal = validate_core(bad, CFG, RAILS)
            with self.assertRaises(SignalValidationError):
                meteora_validator.validate(signal, CFG, RAILS)

    def test_rebalance_side_x_with_y_amount_rejected(self):
        bad = _valid_rebalance(side="x", liquidity={"amount_x": "0", "amount_y": "1000"})
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            with self.assertRaises(SignalValidationError):
                validate_core(bad, CFG, RAILS)

    def test_remove_bps_below_partial_min_rejected(self):
        bad = _valid_remove(bps=2000)
        with self.assertRaises(SignalValidationError) as ctx:
            validate_core(bad, CFG, RAILS)
        self.assertIn("partial-remove range", str(ctx.exception))

    def test_remove_bps_above_partial_max_rejected(self):
        bad = _valid_remove(bps=8000)
        with self.assertRaises(SignalValidationError):
            validate_core(bad, CFG, RAILS)


class TestRebalanceRotateBuilder(unittest.TestCase):
    def test_rebalance_request_mapping(self):
        req = meteora_builder.build(_valid_rebalance(), CFG, RAILS)
        self.assertEqual(req["action"], "rebalance")
        self.assertEqual(req["position_id"], "43ivjtQ7s8AweC2suoAULYAhPVtQRapAfsahdXDDs1QM")
        self.assertEqual(req["bin_range"], {"lower": -2156, "upper": -2108})
        self.assertEqual(req["side"], "bidirectional")

    def test_rotate_request_mapping(self):
        req = meteora_builder.build(_valid_rotate(side="x", liquidity={"amount_x": "1000", "amount_y": "0"}), CFG, RAILS)
        self.assertEqual(req["action"], "rotate")
        self.assertEqual(req["side"], "x")


class TestAdaptiveThreshold(unittest.TestCase):
    def test_open_below_static_score_policy_rejected(self):
        sig = {
            "signal_id": "adaptive-test",
            "action": "open",
            "dex": "meteora",
            "pool_address": "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y",
            "side": "bidirectional",
            "bin_range": {"lower": -2156, "upper": -2108},
            "liquidity": {"amount_x": "1000", "amount_y": "1000"},
            "position_usd": 50.0,
            "score": 68.0,
            "score_policy": {"source": "missy", "version": 1, "min_open_score": 70.0},
            "created_at": _now_iso(),
            "max_slippage_bps": 100,
        }
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            with self.assertRaises(SignalValidationError) as ctx:
                validate_core(sig, CFG, RAILS)
        self.assertIn("min_open_score", str(ctx.exception))

    def test_open_at_adaptive_threshold_passes(self):
        sig = {
            "signal_id": "adaptive-test",
            "action": "open",
            "dex": "meteora",
            "pool_address": "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y",
            "side": "bidirectional",
            "bin_range": {"lower": -2156, "upper": -2108},
            "liquidity": {"amount_x": "1000", "amount_y": "1000"},
            "position_usd": 50.0,
            "score": 60.0,
            "score_policy": {"source": "missy", "version": 1, "min_open_score": 55.0},
            "created_at": _now_iso(),
            "max_slippage_bps": 100,
        }
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            out = validate_core(sig, CFG, RAILS)
        self.assertEqual(out["action"], "open")

    def test_open_adaptive_floor_clamps_low_policy(self):
        sig = {
            "signal_id": "adaptive-test",
            "action": "open",
            "dex": "meteora",
            "pool_address": "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y",
            "side": "bidirectional",
            "bin_range": {"lower": -2156, "upper": -2108},
            "liquidity": {"amount_x": "1000", "amount_y": "1000"},
            "position_usd": 50.0,
            "score": 50.0,
            "score_policy": {"source": "missy", "version": 1, "min_open_score": 45.0},
            "created_at": _now_iso(),
            "max_slippage_bps": 100,
        }
        with patch.object(signal_validator, "fetch_pool_current_tick", return_value=-2127):
            with self.assertRaises(SignalValidationError) as ctx:
                validate_core(sig, CFG, RAILS)
        self.assertIn("55", str(ctx.exception))


class TestMutationCooldown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        state_path = Path(self._tmp.name) / "exposure_state.json"
        state_path.write_text(
            '{"open_positions": [{"pool_address": "POOL", "position_id": "POS1", '
            '"position_usd": 50.0, "opened_at": "2026-10-01T00:00:00+00:00"}],'
            ' "daily_loss_usd": 0.0, "max_daily_loss_usd": 0.0,'
            ' "peak_portfolio_usd": 0.0, "session_start_value_usd": 0.0,'
            ' "last_reset_date": "2026-10-01", "last_mutations": {}}'
        )
        patcher = patch.object(exposure_guard, "STATE_PATH", state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_cooldown_blocks_repeated_add(self):
        signal = {"pool_address": "POOL", "position_id": "POS1", "position_usd": 10.0, "action": "add_liquidity"}
        exposure_guard.record_add_executed(signal)
        allowed, reason = exposure_guard.check_add_allowed(signal, RAILS)
        self.assertFalse(allowed)
        self.assertIn("cooldown", reason)

    def test_cooldown_allows_after_elapsed(self):
        signal = {"pool_address": "POOL", "position_id": "POS1", "position_usd": 10.0, "action": "add_liquidity"}
        # Record an old mutation manually.
        state = exposure_guard.load_state()
        state["last_mutations"] = {
            "POS1": {"at": (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(), "action": "add_liquidity"}
        }
        exposure_guard.save_state(state)
        allowed, reason = exposure_guard.check_add_allowed(signal, RAILS)
        self.assertTrue(allowed, reason)

    def test_remove_remaining_below_min_rejected(self):
        signal = {
            "pool_address": "POOL",
            "position_id": "POS1",
            "bps": 7500,
            "position_usd": 20.0,
            "action": "remove_liquidity",
        }
        allowed, reason = exposure_guard.check_remove_allowed(signal, RAILS)
        self.assertFalse(allowed)
        self.assertIn("remaining", reason)

    def test_rebalance_net_exposure_respected(self):
        signal = {
            "pool_address": "POOL",
            "position_id": "POS1",
            "position_usd": 500.0,
            "action": "rebalance",
        }
        allowed, reason = exposure_guard.check_rebalance_allowed(signal, RAILS)
        self.assertFalse(allowed)
        self.assertIn("position_usd", reason)

    def test_rotate_new_size_fits(self):
        signal = {
            "pool_address": "POOL",
            "position_id": "POS1",
            "position_usd": 50.0,
            "action": "rotate",
        }
        allowed, reason = exposure_guard.check_rotate_allowed(signal, RAILS)
        self.assertTrue(allowed, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
