#!/usr/bin/env python3
"""Runtime-freshness tests: the daemon must not trade on a stale snapshot.

SAFETY_RAILS.md states that the executor "re-reads it before every
transaction". Two holes were found in the 2026-10-29 audit:

  1. the daemon loop loaded cfg+rails once at start-up, so an owner edit (mode
     change, tightened cap) had no effect until a restart;
  2. the journal's rails hash was cached for the life of the process, so
     entries kept proving compliance against a rails file that had changed.

These tests pin both. They are hermetic: no RPC, no signals, no chain calls.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dispatcher  # noqa: E402


class RailsHashFreshnessTests(unittest.TestCase):
    def setUp(self):
        dispatcher._rails_hash_cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "SAFETY_RAILS.md"
        self.path.write_text("# rails v1\nmax_daily_loss_usd | **10**\n",
                             encoding="utf-8")

    def test_hash_is_stable_while_the_file_is(self):
        first = dispatcher.rails_hash_cached(str(self.path))
        second = dispatcher.rails_hash_cached(str(self.path))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_hash_changes_when_the_rails_change(self):
        before = dispatcher.rails_hash_cached(str(self.path))
        self.path.write_text("# rails v2\nmax_daily_loss_usd | **5**\n",
                             encoding="utf-8")
        # Filesystem mtime resolution can be coarse; make the change explicit.
        os.utime(self.path, (os.path.getatime(self.path),
                             os.path.getmtime(self.path) + 1))
        after = dispatcher.rails_hash_cached(str(self.path))
        self.assertNotEqual(before, after)

    def test_missing_file_hashes_to_empty(self):
        self.assertEqual(dispatcher.rails_hash_cached("/nonexistent/rails.md"), "")


class DaemonReloadTests(unittest.TestCase):
    """main()'s loop must ask for cfg+rails again on every iteration."""

    def _run_one_iteration(self):
        """Run the daemon loop body exactly once, then break out."""
        calls = {"cfg": 0, "rails": 0, "epilogue": 0}

        def fake_load_config():
            calls["cfg"] += 1
            return {"mode": "dry_run", "heartbeat_interval_seconds": 1}

        def fake_load_rails():
            calls["rails"] += 1
            return {"max_daily_loss_usd": 10.0}

        def fake_run_once(cfg, rails):
            calls["epilogue"] += 1
            raise KeyboardInterrupt  # leave the infinite loop after one pass

        with mock.patch.object(dispatcher, "load_config", fake_load_config), \
                mock.patch.object(dispatcher, "load_rails", fake_load_rails), \
                mock.patch.object(dispatcher, "run_once", fake_run_once), \
                mock.patch.object(dispatcher, "_capital_epilogue",
                                  lambda cfg, executed: False), \
                mock.patch.object(dispatcher.time, "sleep", lambda *_: None), \
                mock.patch.object(sys, "argv", ["dispatcher.py"]):
            with self.assertRaises(KeyboardInterrupt):
                dispatcher.main()
        return calls

    def test_loop_reloads_config_and_rails(self):
        calls = self._run_one_iteration()
        # One load before the CLI branches, one per daemon iteration: the
        # iteration count is what proves the reload.
        self.assertEqual(calls["cfg"], 2)
        self.assertEqual(calls["rails"], 2)
        self.assertEqual(calls["epilogue"], 1)

    def test_unreadable_config_idles_instead_of_trading(self):
        """A bad mid-run edit must not execute anything, and must not kill the loop."""
        from common.config_loader import ConfigError

        state = {"calls": 0, "ran": 0}

        def flaky_load_config():
            state["calls"] += 1
            if state["calls"] == 1:
                return {"mode": "dry_run", "heartbeat_interval_seconds": 1}
            if state["calls"] == 2:
                # The file became unreadable between wake 1 and wake 2.
                raise ConfigError("rpc.https_url is required")
            raise KeyboardInterrupt

        def fail_if_run(*_a, **_k):
            state["ran"] += 1
            return []

        with mock.patch.object(dispatcher, "load_config", flaky_load_config), \
                mock.patch.object(dispatcher, "load_rails", lambda: {}), \
                mock.patch.object(dispatcher, "run_once", fail_if_run), \
                mock.patch.object(dispatcher.time, "sleep", lambda *_: None), \
                mock.patch.object(sys, "argv", ["dispatcher.py"]):
            with self.assertRaises(KeyboardInterrupt):
                dispatcher.main()
        self.assertEqual(state["ran"], 0, "an unreadable config must not run a wake")
        self.assertEqual(state["calls"], 3, "the loop must keep going after a bad read")


if __name__ == "__main__":
    unittest.main(verbosity=2)
