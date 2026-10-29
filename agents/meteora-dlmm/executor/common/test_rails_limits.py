#!/usr/bin/env python3
"""Loss / drawdown / exposure breaker accounting.

Owner-approved limits (execution_limits.json, the machine-readable source of
truth): $100 per position, $500 total exposure, $10 daily loss, 10% drawdown.

These tests pin BOTH halves of the contract:
  1. the file really contains those numbers (a silent edit is caught), and
  2. ``check_open_allowed`` actually enforces each one at its boundary.

They are hermetic: the guard state is redirected to a temp file, so nothing
touches the live ``state/exposure_state.json``.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import exposure_guard as guard  # noqa: E402
from common import rails_loader  # noqa: E402

LIMITS = {
    "max_position_usd": 100.0,
    "max_total_exposure_usd": 500.0,
    "max_daily_loss_usd": 10.0,
    "max_drawdown_pct": 10.0,
}


class LimitsFileTests(unittest.TestCase):
    """The configured numbers must be the approved ones."""

    @classmethod
    def setUpClass(cls):
        cls.path = rails_loader.EXECUTION_LIMITS_PATH
        cls.data = json.loads(cls.path.read_text(encoding="utf-8"))

    def test_file_exists(self):
        self.assertTrue(self.path.exists(), self.path)

    def test_position_cap_is_100(self):
        self.assertEqual(
            float(self.data["position_sizing"]["max_position_usd"]),
            LIMITS["max_position_usd"],
        )

    def test_total_exposure_cap_is_500(self):
        self.assertEqual(
            float(self.data["exposure_and_loss"]["max_total_exposure_usd"]),
            LIMITS["max_total_exposure_usd"],
        )

    def test_daily_loss_cap_is_10(self):
        self.assertEqual(
            float(self.data["exposure_and_loss"]["max_daily_loss_usd"]),
            LIMITS["max_daily_loss_usd"],
        )

    def test_drawdown_cap_is_10pct(self):
        self.assertEqual(
            float(self.data["exposure_and_loss"]["max_drawdown_pct"]),
            LIMITS["max_drawdown_pct"],
        )

    def test_rails_loader_exposes_the_same_numbers(self):
        rails = rails_loader.load_rails()
        for key, expected in LIMITS.items():
            self.assertEqual(float(rails[key]), expected, key)

    def test_live_authorization_was_not_expanded(self):
        """Previous live authorization was ~$20; caps must not exceed it."""
        self.assertLessEqual(
            float(self.data["position_sizing"]["max_position_usd"]), 100.0
        )
        self.assertLessEqual(
            float(self.data["exposure_and_loss"]["max_total_exposure_usd"]), 500.0
        )


class GuardEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state_path = self.dir / "exposure_state.json"
        self._patch = mock.patch.object(guard, "STATE_PATH", self.state_path)
        self._patch.start()
        self.rails = rails_loader.load_rails()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _state(self, **kw):
        base = {
            "open_positions": [],
            "daily_loss_usd": 0.0,
            "peak_portfolio_usd": 0.0,
            "session_start_value_usd": 0.0,
            "last_reset_date": guard._today(),
        }
        base.update(kw)
        guard.save_state(base)

    def _open(self, usd, wallet="main"):
        return guard.check_open_allowed(
            {"position_usd": usd, "wallet_id": wallet}, self.rails
        )

    # --- per-position cap -------------------------------------------------
    def test_open_at_cap_allowed(self):
        self._state()
        allowed, reason = self._open(100.0)
        self.assertTrue(allowed, reason)

    def test_open_above_cap_rejected(self):
        self._state()
        allowed, reason = self._open(100.01)
        self.assertFalse(allowed)
        self.assertIn("> max 100.00", reason)

    # --- total exposure ---------------------------------------------------
    def test_total_exposure_at_cap_allowed(self):
        self._state(open_positions=[{"position_usd": 400.0, "wallet_id": "main"}])
        allowed, reason = self._open(100.0)
        self.assertTrue(allowed, reason)

    def test_total_exposure_overflow_rejected(self):
        self._state(open_positions=[{"position_usd": 450.0, "wallet_id": "main"}])
        allowed, reason = self._open(100.0)
        self.assertFalse(allowed)
        self.assertIn("exposure", reason)
        self.assertIn("500.00", reason)

    def test_exposure_is_per_wallet(self):
        """A mirror's usage must not consume MAIN's budget."""
        self._state(open_positions=[{"position_usd": 450.0, "wallet_id": "mirror"}])
        allowed, reason = self._open(100.0, wallet="main")
        self.assertTrue(allowed, reason)

    # --- daily loss -------------------------------------------------------
    def test_daily_loss_below_cap_allowed(self):
        self._state(daily_loss_usd=9.99)
        allowed, reason = self._open(50.0)
        self.assertTrue(allowed, reason)

    def test_daily_loss_at_cap_rejected(self):
        self._state(daily_loss_usd=10.0)
        allowed, reason = self._open(50.0)
        self.assertFalse(allowed)
        self.assertIn("daily loss", reason)

    def test_daily_loss_over_cap_rejected(self):
        self._state(daily_loss_usd=10.01)
        allowed, _ = self._open(10.0)
        self.assertFalse(allowed)

    # --- drawdown ---------------------------------------------------------
    def test_drawdown_below_cap_allowed(self):
        self._state(peak_portfolio_usd=1000.0, session_start_value_usd=901.0)
        allowed, reason = self._open(50.0)
        self.assertTrue(allowed, reason)

    def test_drawdown_at_cap_rejected(self):
        self._state(peak_portfolio_usd=1000.0, session_start_value_usd=900.0)
        allowed, reason = self._open(50.0)
        self.assertFalse(allowed)
        self.assertIn("drawdown", reason)

    def test_drawdown_without_peak_is_zero(self):
        self._state(peak_portfolio_usd=0.0, session_start_value_usd=0.0)
        allowed, reason = self._open(50.0)
        self.assertTrue(allowed, reason)

    # --- fail closed ------------------------------------------------------
    def test_missing_position_usd_fails_closed(self):
        self._state()
        allowed, reason = guard.check_open_allowed({"wallet_id": "main"}, self.rails)
        self.assertFalse(allowed)
        self.assertIn("fail-closed", reason)

    def test_below_min_position_rejected(self):
        self._state()
        allowed, reason = self._open(5.0)
        self.assertFalse(allowed)
        self.assertIn("< min", reason)

    def test_max_open_positions_enforced(self):
        self._state(
            open_positions=[{"position_usd": 10.0, "wallet_id": "main"}] * 9
        )
        allowed, reason = self._open(10.0)
        self.assertFalse(allowed)
        self.assertIn("open positions", reason)


class FallbackStrictnessTests(unittest.TestCase):
    """A missing limits file must NARROW the executor, never widen it.

    Before the 2026-10-29 audit, DEFAULT_RAILS was looser than the live file in
    three places (daily loss 50 vs 10, min_open_score 70 vs 90, exposure 300 vs
    500 for the same key that lives at 500), so one unreadable file silently
    disabled most of the loss rail.
    """

    # Direction per rail. "cap" = a smaller value is stricter (it forbids
    # more); "floor" = a larger value is stricter (it demands more).
    CAPS = (
        "max_position_usd", "max_total_exposure_usd", "max_open_positions",
        "max_daily_loss_usd", "max_drawdown_pct", "max_slippage_bps",
        "partial_remove_bps_max", "max_price_impact_pct",
        "priority_fee_cap_sol", "tx_timeout_seconds",
        "max_bin_range_width", "max_meteora_range_width",
        "max_orca_range_width", "max_raydium_range_width",
        "pause_after_consecutive_failures", "pause_on_rpc_error_streak",
    )
    FLOORS = (
        "min_position_usd", "min_open_score", "min_open_score_adaptive_floor",
        "min_remove_bps", "partial_remove_bps_min", "claim_min_usd",
        "mutation_cooldown_hours", "min_pool_liquidity_usd",
        "min_24h_volume_usd",
    )

    @classmethod
    def setUpClass(cls):
        cls.limits = rails_loader._load_execution_limits()

    def test_every_cap_fallback_is_at_least_as_strict(self):
        for key in self.CAPS:
            if key not in self.limits:
                continue
            with self.subTest(rail=key):
                self.assertLessEqual(
                    float(rails_loader.DEFAULT_RAILS[key]),
                    float(self.limits[key]),
                    f"fallback {key} is looser than execution_limits.json",
                )

    def test_every_floor_fallback_is_at_least_as_strict(self):
        for key in self.FLOORS:
            if key not in self.limits:
                continue
            with self.subTest(rail=key):
                self.assertGreaterEqual(
                    float(rails_loader.DEFAULT_RAILS[key]),
                    float(self.limits[key]),
                    f"fallback {key} is looser than execution_limits.json",
                )

    def test_bin_step_rule_fallback_is_not_wider(self):
        live = self.limits["bin_step_rule"]
        fallback = rails_loader.DEFAULT_RAILS["bin_step_rule"]
        self.assertGreaterEqual(fallback["min"], live["min"])
        self.assertLessEqual(fallback["max"], live["max"])

    def test_markdown_cannot_widen_a_mechanical_limit(self):
        """SAFETY_RAILS.md is documentation; editing it must not move a rail."""
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "SAFETY_RAILS.md"
            md.write_text(
                "| `max_daily_loss_usd` | **1000** | pretend rail |\n"
                "| `min_open_score` | **1** | pretend rail |\n",
                encoding="utf-8",
            )
            with mock.patch.object(rails_loader, "RAILS_PATH", md):
                rails = rails_loader.load_rails()
        self.assertLessEqual(float(rails["max_daily_loss_usd"]),
                             float(self.limits["max_daily_loss_usd"]))
        self.assertGreaterEqual(float(rails["min_open_score"]),
                                float(self.limits["min_open_score"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
