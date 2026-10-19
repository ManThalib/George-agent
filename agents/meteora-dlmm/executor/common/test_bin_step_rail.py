"""Executor-side bin_step rail: rule from execution_limits.json.

Owner ruling 2026-10-19: Meteora bin_step >= 4 allowed, > 400 protocol-invalid.
`execution_limits.json` is the single source of truth for George's rails; the
Sheldon policy copy no longer overrides it.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.bin_step_rail import (  # noqa: E402
    METEORA_MAX_BIN_STEP,
    METEORA_MIN_BIN_STEP,
    meteora_bin_step_allowed,
    normalize_rule,
)
from common.rails_loader import load_rails  # noqa: E402


class PredicateTests(unittest.TestCase):
    def test_allowed_steps(self):
        for step in (4, 10, 20, 25, 50, 100, 400):
            self.assertTrue(meteora_bin_step_allowed(step)[0], step)

    def test_rejected_below_minimum(self):
        for step in (1, 2, 3):
            ok, reason = meteora_bin_step_allowed(step)
            self.assertFalse(ok, step)
            self.assertIn("below owner minimum", reason)

    def test_rejected_above_protocol_max(self):
        for step in (401, 999):
            ok, reason = meteora_bin_step_allowed(step)
            self.assertFalse(ok, step)
            self.assertIn("protocol maximum", reason)

    def test_fail_closed_values(self):
        for value in (None, "", 0, "junk", False):
            self.assertFalse(meteora_bin_step_allowed(value)[0], repr(value))

    def test_rule_bounds_cannot_be_widened(self):
        self.assertEqual(normalize_rule({"min": 1, "max": 9999}),
                         {"min": METEORA_MIN_BIN_STEP,
                          "max": METEORA_MAX_BIN_STEP})


class RailSourceTests(unittest.TestCase):
    def test_execution_limits_is_the_source(self):
        rails = load_rails()
        self.assertEqual(rails.get("bin_step_rule"), {"min": 4, "max": 400})
        self.assertIsNone(rails.get("allowed_bin_steps"),
                          "stale list must not survive in the merged rails")

    def test_sheldon_policy_cannot_override_george_rail(self):
        rails = load_rails()
        # Whatever Sheldon's policy says, George's merged rule is the owner one.
        self.assertEqual(normalize_rule(rails.get("bin_step_rule")),
                         {"min": 4, "max": 400})


class ValidatorTests(unittest.TestCase):
    """The validator must enforce the rule, not a hardcoded whitelist."""

    def _validate(self, bin_step, rails=None):
        from unittest.mock import patch
        from dexes.meteora import validator

        rails = rails if rails is not None else load_rails()
        signal = {"action": "open", "dex": "meteora",
                  "pool_address": "P1", "wallet_id": "main"}
        with patch.object(validator.signal_validator, "fetch_pool_bin_step",
                          return_value=bin_step):
            with patch.object(validator, "validate_core",
                              side_effect=lambda s, c, r: s):
                return validator.validate(signal, {"rpc_https_url": "x"}, rails)

    def test_validator_accepts_4_and_400(self):
        for step in (4, 400):
            self.assertIsNotNone(self._validate(step))

    def test_validator_rejects_1_2_3(self):
        from common.signal_validator import SignalValidationError
        for step in (1, 2, 3):
            with self.assertRaises(SignalValidationError, msg=str(step)):
                self._validate(step)

    def test_validator_rejects_401(self):
        from common.signal_validator import SignalValidationError
        with self.assertRaises(SignalValidationError):
            self._validate(401)

    def test_validator_fails_closed_when_fetch_fails(self):
        from common.signal_validator import SignalValidationError
        with self.assertRaises(SignalValidationError):
            self._validate(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
