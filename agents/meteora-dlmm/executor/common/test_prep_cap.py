#!/usr/bin/env python3
"""Tests for the per-wake prep cap (bounded batch)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dispatcher  # noqa: E402
from common import signal_validator  # noqa: E402

CFG = {"signal_max_age_seconds": 300}


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

    def _write(self, name, action="swap", age_seconds=0):
        created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        (self.pending / name).write_text(json.dumps({
            "signal_id": name[:-5],
            "action": action,
            "created_at": created.isoformat(),
        }))

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
            dispatcher.run_once({}, CFG)
        self.assertEqual(self.calls, ["sheldon-close-1.json"])

    def test_stale_prep_does_not_consume_the_slot(self):
        """A stale leftover must not starve the fresh prep behind it.

        Pending sorts by name, and prep names embed the creation epoch, so a
        leftover backlog always sorts ahead of the current prep.
        """
        self._write("sheldon-prep-1-1.json", age_seconds=7200)   # stale, sorts first
        self._write("sheldon-prep-2-1.json", age_seconds=0)      # fresh
        with mock.patch.object(dispatcher, "process_signal_path",
                               side_effect=self._fake_process), \
             mock.patch.object(dispatcher, "_reconcile_exposure", lambda: None):
            dispatcher.run_once({}, CFG)
        # Both are attempted (the stale one only so it leaves pending), but the
        # cap slot belongs to the fresh prep.
        self.assertIn("sheldon-prep-2-1.json", self.calls)

    def test_stale_backlog_cannot_block_a_fresh_prep(self):
        for i in range(1, 4):
            self._write(f"sheldon-prep-{i}-1.json", age_seconds=7200)
        self._write("sheldon-prep-9-1.json", age_seconds=0)
        with mock.patch.object(dispatcher, "process_signal_path",
                               side_effect=self._fake_process), \
             mock.patch.object(dispatcher, "_reconcile_exposure", lambda: None):
            dispatcher.run_once({}, CFG)
        self.assertIn("sheldon-prep-9-1.json", self.calls)

    def test_fresh_preps_are_still_capped(self):
        self._write("sheldon-prep-1-1.json", age_seconds=10)
        self._write("sheldon-prep-2-1.json", age_seconds=5)
        with mock.patch.object(dispatcher, "process_signal_path",
                               side_effect=self._fake_process), \
             mock.patch.object(dispatcher, "_reconcile_exposure", lambda: None):
            dispatcher.run_once({}, CFG)
        preps = [c for c in self.calls if c.startswith("sheldon-prep-")]
        self.assertEqual(len(preps), 1)


class PrepFreshnessHelperTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cfg = {"signal_max_age_seconds": 300}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _path(self, payload):
        p = self.root / "sheldon-prep-1-1.json"
        p.write_text(json.dumps(payload))
        return p

    def test_fresh_signal(self):
        p = self._path({"created_at": datetime.now(timezone.utc).isoformat()})
        self.assertTrue(dispatcher._prep_is_fresh(p, self.cfg))

    def test_old_signal_is_not_fresh(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=7200)
        self.assertFalse(dispatcher._prep_is_fresh(
            self._path({"created_at": old.isoformat()}), self.cfg))

    def test_asia_shanghai_offset_is_understood(self):
        """Sheldon writes +08:00 timestamps; they must not read as stale."""
        now_local = datetime.now(timezone(timedelta(hours=8)))
        self.assertTrue(dispatcher._prep_is_fresh(
            self._path({"created_at": now_local.isoformat()}), self.cfg))

    def test_unusable_age_is_left_to_validation(self):
        self.assertTrue(dispatcher._prep_is_fresh(self._path({}), self.cfg))
        self.assertTrue(dispatcher._prep_is_fresh(
            self._path({"created_at": "not-a-date"}), self.cfg))

    def test_unreadable_file_is_left_to_validation(self):
        self.assertTrue(dispatcher._prep_is_fresh(
            self.root / "missing.json", self.cfg))


class SwapFreshnessGateTests(unittest.TestCase):
    """SAFETY_RAILS.md: signals older than signal_max_age_seconds are stale.

    validate_core returned early for `swap` without calling _check_freshness,
    so prep swaps (action: swap) had no freshness gate at all: a backlogged
    prep executed with an amount computed for a long-gone balance.
    """

    CFG = {"signal_max_age_seconds": 300, "rpc_https_url": "https://rpc.test"}
    RAILS = {"max_slippage_bps": 100}

    def _swap(self, age_seconds):
        created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return {
            "signal_id": "sheldon-prep-1-1",
            "action": "swap",
            "input_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "output_mint": "So11111111111111111111111111111111111111112",
            "amount": "1000000",
            "max_slippage_bps": 100,
            "created_at": created.isoformat(),
        }

    def test_fresh_swap_passes(self):
        signal_validator.validate_core(self._swap(5), self.CFG, self.RAILS)

    def test_stale_swap_is_rejected(self):
        with self.assertRaises(signal_validator.SignalValidationError) as ctx:
            signal_validator.validate_core(self._swap(7200), self.CFG, self.RAILS)
        self.assertIn("stale", str(ctx.exception))

    def test_boundary_just_inside_the_window_passes(self):
        signal_validator.validate_core(self._swap(299), self.CFG, self.RAILS)

    def test_signal_age_seconds_reads_offsets(self):
        now_local = datetime.now(timezone(timedelta(hours=8)))
        age = signal_validator.signal_age_seconds(
            {"created_at": now_local.isoformat()})
        self.assertIsNotNone(age)
        self.assertLess(abs(age), 5)

    def test_signal_age_seconds_none_when_unusable(self):
        for payload in ({}, {"created_at": None}, {"created_at": "nope"}):
            self.assertIsNone(signal_validator.signal_age_seconds(payload))


class RunOnceStatusRoutingTests(unittest.TestCase):
    """Every status must leave pending/ — including one nobody has added yet.

    run_once used to gate the move on a set literal listing every known status
    and bind `readback` inside it, so a new status raised UnboundLocalError
    mid-batch and left the rest of the queue unprocessed.
    """

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
        dispatcher.KILL_FILE = self.root / "KILL"
        dispatcher.LOCK_PATH = self.root / "dispatcher.lock"

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(dispatcher, k, v)
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, name):
        (self.pending / name).write_text(json.dumps({
            "signal_id": name[:-5],
            "action": "close",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))

    def test_unrecognised_status_is_still_archived(self):
        self._write("sheldon-close-1.json")
        with mock.patch.object(dispatcher, "process_signal_path",
                               return_value=("a_status_from_the_future", {})), \
                mock.patch.object(dispatcher, "_reconcile_exposure", lambda: None):
            executed = dispatcher.run_once({}, {})
        self.assertEqual(executed, [])
        self.assertEqual(list(self.pending.glob("*.json")), [])
        archived = self.signals / "processed" / "sheldon-close-1.json"
        self.assertTrue(archived.exists())

    def test_executed_status_goes_through_the_self_verify_router(self):
        self._write("sheldon-open-1.json")
        (self.pending / "sheldon-open-1.json").write_text(json.dumps({
            "signal_id": "sheldon-open-1",
            "action": "open",
            "position_id": "POS1",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))
        details = {"result": {"verify": {"ok": False}, "position_id": "POS1"}}
        with mock.patch.object(dispatcher, "process_signal_path",
                               return_value=("executed", details)), \
                mock.patch.object(dispatcher, "_reconcile_exposure", lambda: None), \
                mock.patch.object(dispatcher, "_safe_record_execution_state",
                                  lambda *a, **k: None):
            dispatcher.run_once({}, {})
        # Failed self-verify must route the file to failed_verify/, not processed/.
        failed = self.signals / "failed_verify" / "sheldon-open-1.json"
        self.assertTrue(failed.exists())


if __name__ == "__main__":
    unittest.main()
