#!/usr/bin/env python3
"""Tests for the per-wake prep cap (bounded batch)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dispatcher  # noqa: E402


class PrepCapHelpersTests(unittest.TestCase):
    def test_default_is_one(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEORGE_MAX_PREPS_PER_RUN", None)
            self.assertEqual(dispatcher._max_preps_per_run(), 1)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"GEORGE_MAX_PREPS_PER_RUN": "3"}):
            self.assertEqual(dispatcher._max_preps_per_run(), 3)

    def test_env_garbage_falls_back(self):
        with mock.patch.dict(os.environ, {"GEORGE_MAX_PREPS_PER_RUN": "abc"}):
            self.assertEqual(dispatcher._max_preps_per_run(), 1)

    def test_is_prep_signal_path(self):
        self.assertTrue(dispatcher._is_prep_signal_path(
            Path("sheldon-prep-1791200602-1.json")))
        self.assertFalse(dispatcher._is_prep_signal_path(
            Path("sheldon-open-1791200602-1.json")))
        self.assertFalse(dispatcher._is_prep_signal_path(
            Path("sheldon-close-1.json")))


class RunOncePrepCapTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.signals = self.root / "signals"
        self.pending = self.signals / "pending"
        self.pending.mkdir(parents=True)
        self._orig = {
            "SIGNALS_DIR": dispatcher.SIGNALS_DIR,
            "KILL_FILE": dispatcher.KILL_FILE,
            "LOCK_PATH": dispatcher.LOCK_PATH,
        }
        dispatcher.SIGNALS_DIR = self.signals
        dispatcher.KILL_FILE = self.root / "KILL"  # does not exist
        dispatcher.LOCK_PATH = self.root / "dispatcher.lock"
        self.calls = []

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(dispatcher, k, v)
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, name, action="swap"):
        (self.pending / name).write_text(json.dumps({"signal_id": name[:-5],
                                                     "action": action}))

    def _fake_process(self, signal_path, cfg, rails):
        self.calls.append(signal_path.name)
        return "rejected", {"error": "test"}

    def test_caps_preps_but_not_other_actions(self):
        self._write("sheldon-prep-1-1.json")
        self._write("sheldon-prep-2-1.json")
        self._write("sheldon-prep-3-1.json")
        self._write("sheldon-close-1.json", action="close")
        with mock.patch.object(dispatcher, "process_signal_path",
                               side_effect=self._fake_process), \
             mock.patch.object(dispatcher, "_reconcile_exposure", lambda: None):
            dispatcher.run_once({}, {})
        # Exactly one prep + the close; the other two preps deferred.
        self.assertEqual(self.calls.count("sheldon-close-1.json"), 1)
        preps = [c for c in self.calls if c.startswith("sheldon-prep-")]
        self.assertEqual(len(preps), 1)
        remaining = sorted(p.name for p in self.pending.glob("*.json"))
        self.assertEqual(remaining, ["sheldon-prep-2-1.json", "sheldon-prep-3-1.json"])

    def test_cap_of_zero_defers_all_preps(self):
        self._write("sheldon-prep-1-1.json")
        self._write("sheldon-close-1.json", action="close")
        with mock.patch.dict(os.environ, {"GEORGE_MAX_PREPS_PER_RUN": "0"}), \
             mock.patch.object(dispatcher, "process_signal_path",
                               side_effect=self._fake_process), \
             mock.patch.object(dispatcher, "_reconcile_exposure", lambda: None):
            dispatcher.run_once({}, {})
        self.assertEqual(self.calls, ["sheldon-close-1.json"])


if __name__ == "__main__":
    unittest.main()
