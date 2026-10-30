#!/usr/bin/env python3
"""The failure breaker as the dispatcher uses it.

Unit behaviour lives in test_circuit_breaker.py; this file pins the wiring:
a paused executor processes nothing, a third failure stops the wake mid-batch,
rejections do not count, and `--once` reports the pause with a distinct exit
code instead of looking like a healthy cycle or a crash.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dispatcher  # noqa: E402
from common import circuit_breaker as cb  # noqa: E402

RAILS = {"pause_after_consecutive_failures": 3, "pause_on_rpc_error_streak": 5}


class BreakerWiringTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.signals = self.root / "signals"
        self.pending = self.signals / "pending"
        self.pending.mkdir(parents=True)
        self.state = self.root / "circuit_breaker.json"

        self._patches = [
            mock.patch.object(dispatcher, "SIGNALS_DIR", self.signals),
            mock.patch.object(dispatcher, "KILL_FILE", self.root / "KILL"),
            mock.patch.object(dispatcher, "LOCK_PATH", self.root / "dispatcher.lock"),
            mock.patch.object(cb, "STATE_PATH", self.state),
            mock.patch.object(dispatcher, "_reconcile_exposure", lambda: None),
            mock.patch.object(dispatcher, "_prune_failed_verify", lambda *a, **k: 0),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.notifications = []
        self._notify = mock.patch.object(
            dispatcher, "_notify_owner",
            side_effect=lambda text: self.notifications.append(text))
        self._notify.start()
        self.addCleanup(self._notify.stop)

    def _write(self, name, action="close"):
        (self.pending / name).write_text(json.dumps({
            "signal_id": name[:-5],
            "action": action,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))

    # --- what counts ------------------------------------------------------
    def test_rejections_do_not_count_as_failures(self):
        for _ in range(5):
            dispatcher._record_outcome("rejected", {"error": "rail"}, RAILS)
        self.assertFalse(cb.is_paused()[0])
        self.assertEqual(cb.load()["consecutive_failures"], 0)

    def test_three_failures_pause_and_alert(self):
        for _ in range(3):
            dispatcher._record_outcome("failed", {"error": "boom"}, RAILS)
        self.assertTrue(cb.is_paused()[0])
        self.assertTrue(any("SELF-PAUSE" in n for n in self.notifications))

    def test_rpc_failure_is_classified_from_the_details(self):
        dispatcher._record_outcome(
            "failed", {"stage": "send", "error": "fetch failed: ECONNRESET"}, RAILS)
        self.assertEqual(cb.load()["consecutive_rpc_errors"], 1)

    def test_success_clears_the_streak(self):
        dispatcher._record_outcome("failed", {"error": "boom"}, RAILS)
        dispatcher._record_outcome("executed", {}, RAILS)
        self.assertEqual(cb.load()["consecutive_failures"], 0)

    def test_breaker_failure_never_raises(self):
        with mock.patch.object(cb, "record_failure", side_effect=OSError("disk")):
            dispatcher._record_outcome("failed", {"error": "boom"}, RAILS)

    # --- run_once behaviour ----------------------------------------------
    def test_paused_executor_processes_nothing(self):
        self._write("sheldon-close-1.json")
        cb.record_failure(rpc=False, rails={"pause_after_consecutive_failures": 1})
        with mock.patch.object(dispatcher, "process_signal_path") as process:
            executed = dispatcher.run_once({}, RAILS)
        process.assert_not_called()
        self.assertEqual(executed, [])
        self.assertEqual([p.name for p in self.pending.glob("*.json")],
                         ["sheldon-close-1.json"])

    def test_third_failure_stops_the_wake_and_leaves_the_rest_pending(self):
        for i in range(4):
            self._write(f"sheldon-close-{i}.json")
        calls = []

        def failing(signal_path, cfg, rails):
            calls.append(signal_path.name)
            dispatcher._record_outcome("failed", {"error": "boom"}, rails)
            return "failed", {"error": "boom"}

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with mock.patch.object(dispatcher, "process_signal_path", side_effect=failing):
                dispatcher.run_once({}, RAILS)

        # The third failure trips the breaker; the loop must stop there rather
        # than sign a fourth time.
        self.assertEqual(len(calls), 3)
        self.assertTrue(cb.is_paused()[0])
        self.assertEqual(len(list(self.pending.glob("*.json"))), 1)
        self.assertIn("leaving 1 signal(s) pending", out.getvalue())

    def test_once_exits_76_when_paused(self):
        cb.record_failure(rpc=False, rails={"pause_after_consecutive_failures": 1})
        out = io.StringIO()
        with mock.patch.object(dispatcher, "load_config", lambda: {"mode": "dry_run"}), \
                mock.patch.object(dispatcher, "load_rails", lambda: RAILS), \
                mock.patch.object(dispatcher, "run_once", lambda cfg, rails: []), \
                mock.patch.object(dispatcher, "_capital_epilogue",
                                  lambda cfg, executed: False), \
                mock.patch.object(sys, "argv", ["dispatcher.py", "--once"]):
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as ctx:
                    dispatcher.main()
        self.assertEqual(ctx.exception.code, dispatcher.EXIT_PAUSED)
        self.assertIn("PAUSED", out.getvalue())

    def test_resume_cli_clears_the_pause(self):
        cb.record_failure(rpc=False, rails={"pause_after_consecutive_failures": 1})
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["dispatcher.py", "--resume"]):
            with contextlib.redirect_stdout(out):
                dispatcher.main()
        self.assertFalse(cb.is_paused()[0])
        self.assertIn("RESUMED", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
