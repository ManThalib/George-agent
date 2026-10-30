#!/usr/bin/env python3
"""Consecutive-failure circuit breaker (SAFETY_RAILS.md circuit_breakers).

The rails documented "3 consecutive failures -> self-pause, critical alert" and
"5 consecutive RPC errors -> pause"; the keys were loaded and never read, so a
chain failing every transaction kept signing. These tests pin the behaviour,
including the parts that are easy to get wrong: what counts as a failure, what
breaks a streak, and that nothing resumes automatically.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import circuit_breaker as cb  # noqa: E402

RAILS = {"pause_after_consecutive_failures": 3, "pause_on_rpc_error_streak": 5}


class BreakerStateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "circuit_breaker.json"
        self._patch = mock.patch.object(cb, "STATE_PATH", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    # --- thresholds -------------------------------------------------------
    def test_three_consecutive_failures_trip_the_breaker(self):
        for _ in range(2):
            result = cb.record_failure(rpc=False, rails=RAILS)
            self.assertFalse(result["tripped"])
        result = cb.record_failure(rpc=False, rails=RAILS)
        self.assertTrue(result["tripped"])
        self.assertIn("3 consecutive execution failures", result["reason"])
        paused, reason = cb.is_paused()
        self.assertTrue(paused)
        self.assertIn("pause_after_consecutive_failures", reason)

    def test_a_success_breaks_the_failure_streak(self):
        cb.record_failure(rpc=False, rails=RAILS)
        cb.record_failure(rpc=False, rails=RAILS)
        cb.record_success()
        state = cb.load()
        self.assertEqual(state["consecutive_failures"], 0)
        # Two more failures must not trip on a stale count.
        self.assertFalse(cb.record_failure(rpc=False, rails=RAILS)["tripped"])
        self.assertFalse(cb.record_failure(rpc=False, rails=RAILS)["tripped"])

    def test_five_consecutive_rpc_errors_trip_on_the_rpc_rail(self):
        # Isolate the RPC rail: the failure cap is raised above it, because an
        # RPC error is also a failure and the failure rail would otherwise trip
        # first (which is the shipped behaviour).
        rails = {"pause_after_consecutive_failures": 10,
                 "pause_on_rpc_error_streak": 5}
        for _ in range(4):
            self.assertFalse(cb.record_failure(rpc=True, rails=rails)["tripped"])
        result = cb.record_failure(rpc=True, rails=rails)
        self.assertTrue(result["tripped"])
        self.assertIn("RPC errors", result["reason"])

    def test_a_program_error_breaks_the_rpc_streak_but_not_the_failure_streak(self):
        for _ in range(4):
            cb.record_failure(rpc=True, rails=RAILS)
        result = cb.record_failure(rpc=False, rails=RAILS)
        self.assertEqual(result["consecutive_rpc_errors"], 0)
        self.assertEqual(result["consecutive_failures"], 5)
        self.assertTrue(result["tripped"])  # tripped on the failure rail

    def test_thresholds_come_from_rails_and_a_bad_value_falls_back(self):
        rails = {"pause_after_consecutive_failures": 1}
        self.assertTrue(cb.record_failure(rpc=False, rails=rails)["tripped"])

        cb.resume()
        rails = {"pause_after_consecutive_failures": "nonsense"}
        for _ in range(2):
            self.assertFalse(cb.record_failure(rpc=False, rails=rails)["tripped"])
        self.assertTrue(cb.record_failure(rpc=False, rails=rails)["tripped"])

    # --- stickiness -------------------------------------------------------
    def test_pause_is_sticky_until_an_owner_resumes(self):
        cb.record_failure(rpc=False, rails={"pause_after_consecutive_failures": 1})
        self.assertTrue(cb.is_paused()[0])
        # Successes cannot happen while paused, but even recording one must not
        # silently un-pause: the owner has to look.
        cb.record_success()
        self.assertTrue(cb.is_paused()[0])

    def test_resume_clears_the_pause_and_both_streaks(self):
        cb.record_failure(rpc=True, rails={"pause_after_consecutive_failures": 1})
        state = cb.resume("owner looked at the logs")
        self.assertFalse(state["paused"])
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(state["consecutive_rpc_errors"], 0)
        self.assertFalse(cb.is_paused()[0])
        self.assertIn("resumed_at", state)

    def test_lifetime_failure_count_survives_a_resume(self):
        for _ in range(2):
            cb.record_failure(rpc=False, rails=RAILS)
        cb.resume()
        self.assertEqual(cb.load()["total_failures"], 2)

    # --- persistence ------------------------------------------------------
    def test_state_survives_a_restart(self):
        cb.record_failure(rpc=False, rails={"pause_after_consecutive_failures": 1})
        # Fresh read from disk, as a new process would do.
        raw = json.loads(self.path.read_text())
        self.assertTrue(raw["paused"])
        self.assertTrue(cb.is_paused()[0])

    def test_corrupt_state_is_treated_as_not_paused(self):
        self.path.write_text("{not json", encoding="utf-8")
        paused, _ = cb.is_paused()
        self.assertFalse(paused)
        self.assertEqual(cb.load()["consecutive_failures"], 0)


class RpcClassificationTests(unittest.TestCase):
    def test_transport_failures_look_like_rpc_errors(self):
        for text in ("fetch failed", "503 Service Unavailable",
                     "connect ETIMEDOUT", "BlockhashNotFound",
                     "rate limit exceeded", "RPC request failed"):
            with self.subTest(text=text):
                self.assertTrue(cb.looks_like_rpc_error(text))

    def test_program_failures_do_not(self):
        for text in ("Custom program error: 0x1774",
                     "slippage tolerance exceeded",
                     "meteora closePosition returned no transaction"):
            with self.subTest(text=text):
                self.assertFalse(cb.looks_like_rpc_error(text))


if __name__ == "__main__":
    unittest.main(verbosity=2)
