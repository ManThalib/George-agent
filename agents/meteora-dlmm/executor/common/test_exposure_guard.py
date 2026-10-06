#!/usr/bin/env python3
"""Tests for the exposure guard state machine."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import exposure_guard  # noqa: E402


class RecordClaimExecutedTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.state_dir, "exposure_state.json")

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_claim_does_not_remove_position(self):
        state_path = self.state_path
        exposure_guard.save_state({
            "open_positions": [
                {"pool_address": "pool1", "position_id": "pid1",
                 "wallet_id": "main", "position_usd": 50.0}
            ]
        }, path=state_path)
        exposure_guard.record_claim_executed({
            "action": "claim_fees", "position_id": "pid1",
            "pool_address": "pool1", "wallet_id": "main",
        }, path=Path(state_path))
        state = exposure_guard.load_state(path=state_path)
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertEqual(state["open_positions"][0]["position_id"], "pid1")
        self.assertIn("last_claims", state)

    def test_claim_rewards_does_not_remove_position(self):
        state_path = self.state_path
        exposure_guard.save_state({
            "open_positions": [
                {"pool_address": "pool1", "position_id": "pid1",
                 "wallet_id": "main", "position_usd": 50.0}
            ]
        }, path=state_path)
        exposure_guard.record_claim_executed({
            "action": "claim_rewards", "position_id": "pid1",
        }, path=Path(state_path))
        state = exposure_guard.load_state(path=state_path)
        self.assertEqual(len(state["open_positions"]), 1)


class ReconcilePositionsTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.state_dir, "exposure_state.json")

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def _make_state(self, open_positions=None):
        exposure_guard.save_state({
            "open_positions": open_positions or [],
            "daily_loss_usd": 0.0,
        }, path=self.state_path)

    def test_adds_missing_position(self):
        self._make_state([])
        scan = [{"pool_address": "pool1", "status": "active",
                 "wallet_id": "main", "position_id": "pid1"}]
        now = time.time()
        report = exposure_guard.reconcile_positions(
            scan, scan_mtime=now - 60, now=now, state_dir=self.state_dir)
        self.assertEqual(len(report["added"]), 1)
        state = exposure_guard.load_state(path=self.state_path)
        self.assertEqual(state["open_positions"][0]["pool_address"], "pool1")

    def test_keeps_existing_position(self):
        self._make_state([{"pool_address": "pool1", "wallet_id": "main",
                           "position_usd": 50.0}])
        scan = [{"pool_address": "pool1", "status": "active",
                 "wallet_id": "main"}]
        now = time.time()
        report = exposure_guard.reconcile_positions(
            scan, scan_mtime=now - 60, now=now, state_dir=self.state_dir)
        self.assertEqual(report["added"], [])
        state = exposure_guard.load_state(path=self.state_path)
        self.assertEqual(len(state["open_positions"]), 1)

    def test_unmatched_tracked_position_is_not_removed(self):
        self._make_state([{"pool_address": "gone", "wallet_id": "main",
                           "position_usd": 50.0}])
        scan = [{"pool_address": "still_here", "status": "active",
                 "wallet_id": "main"}]
        now = time.time()
        report = exposure_guard.reconcile_positions(
            scan, scan_mtime=now - 60, now=now, state_dir=self.state_dir)
        self.assertEqual(len(report["added"]), 1)
        self.assertEqual(len(report["unmatched"]), 1)
        state = exposure_guard.load_state(path=self.state_path)
        # gone is kept (conservative) to avoid loosening rails
        self.assertEqual(len(state["open_positions"]), 2)

    def test_stale_scan_skips(self):
        self._make_state([])
        now = time.time()
        report = exposure_guard.reconcile_positions(
            [], scan_mtime=now - 99999, now=now, max_age_seconds=7200,
            state_dir=self.state_dir)
        self.assertIn("stale", report["skipped"])

    def test_empty_scan_returns_no_positions(self):
        self._make_state([])
        now = time.time()
        report = exposure_guard.reconcile_positions(
            [], scan_mtime=now - 60, now=now, state_dir=self.state_dir)
        self.assertIn("no positions", report["skipped"])

    def test_dry_run_does_not_write(self):
        self._make_state([])
        scan = [{"pool_address": "pool1", "status": "active",
                 "wallet_id": "main"}]
        now = time.time()
        report = exposure_guard.reconcile_positions(
            scan, scan_mtime=now - 60, now=now, state_dir=self.state_dir,
            dry_run=True)
        self.assertEqual(len(report["added"]), 1)
        state = exposure_guard.load_state(path=self.state_path)
        self.assertEqual(state["open_positions"], [])


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_backfill_dry_run_preserves_old(self):
        state_path = os.path.join(self.state_dir, "exposure_state.json")
        exposure_guard.save_state({
            "open_positions": [
                {"pool_address": "old", "wallet_id": "main",
                 "position_usd": 100.0}
            ],
            "daily_loss_usd": 12.0,
        }, path=state_path)
        scan = [{"pool_address": "pool1", "status": "active",
                 "wallet_id": "main"}]
        report = exposure_guard.backfill_from_scan(scan, state_dir=self.state_dir,
                                                    dry_run=True)
        self.assertEqual(len(report["after"]), 1)
        self.assertEqual(report["written"], False)
        state = exposure_guard.load_state(path=state_path)
        self.assertEqual(state["open_positions"][0]["pool_address"], "old")
        self.assertEqual(state["daily_loss_usd"], 12.0)

    def test_backfill_replaces_and_writes(self):
        state_path = os.path.join(self.state_dir, "exposure_state.json")
        exposure_guard.save_state({
            "open_positions": [
                {"pool_address": "old", "wallet_id": "main"}
            ],
        }, path=state_path)
        scan = [{"pool_address": "pool1", "status": "active",
                 "wallet_id": "main", "position_usd": 50.0}]
        report = exposure_guard.backfill_from_scan(scan, state_dir=self.state_dir,
                                                    dry_run=False)
        self.assertEqual(len(report["after"]), 1)
        self.assertEqual(report["written"], True)
        state = exposure_guard.load_state(path=state_path)
        self.assertEqual(state["open_positions"][0]["pool_address"], "pool1")


if __name__ == "__main__":
    unittest.main()
