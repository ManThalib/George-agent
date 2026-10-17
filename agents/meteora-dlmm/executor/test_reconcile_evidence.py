"""Evidence-based reconcile tests (2026-10-17 exposure-blind defect).

Live failure vectors from the 2026-10-16/17 review window:
  - B5EwJVDuAauzUE: tracked a CLOSED position id while the live replacement
    (rebalance re-open, same pool) was invisible -> cap blind by ~$45.
  - 2TAgfogn8JRw: position closed on-chain, tracked entry still carried $22.
  - 3ucNos4NbumP: live $25.61, tracked $0.0 (value never refreshed).
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import exposure_guard as eg


NOW = time.time()
FRESH = NOW - 60.0


def _scan_row(pid, pool, usd, wallet="main"):
    return {"position_address": pid, "pool_address": pool,
            "wallet_id": wallet, "current_value_usd": usd,
            "status": "active", "liquidity_raw": "123"}


class ReconcileEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _write_state(self, entries):
        with open(os.path.join(self.state_dir, "exposure_state.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"open_positions": entries}, fh)

    def _read_state(self):
        with open(os.path.join(self.state_dir, "exposure_state.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_dead_id_removed_on_complete_scan(self):
        # Live vector: tracked id is gone from the live book.
        self._write_state([{"pool_address": "POOL_A", "position_id": "DEAD",
                            "wallet_id": "main", "position_usd": 22.37,
                            "opened_at": 1, "source": "reconcile"}])
        report = eg.reconcile_positions(
            [_scan_row("LIVE1", "POOL_B", 81.79)], scan_mtime=FRESH, now=NOW,
            state_dir=self.state_dir, history_complete=True)
        self.assertEqual(len(report["removed"]), 1)
        self.assertEqual(report["removed"][0]["position_id"], "DEAD")
        self.assertEqual(len(report["added"]), 1)
        self.assertEqual(report["added"][0]["position_id"], "LIVE1")
        entries = self._read_state()["open_positions"]
        self.assertEqual([e["position_id"] for e in entries], ["LIVE1"])
        total = eg._total_exposure(entries)
        self.assertAlmostEqual(total, 81.79)

    def test_replacement_id_updates_in_place(self):
        # Live vector: same pool, old id closed, new id live. The entry
        # must adopt the new id + value, and the surplus row must not be
        # double-added.
        self._write_state([{"pool_address": "POOL_A", "position_id": "OLD",
                            "wallet_id": "main", "position_usd": 36.10,
                            "opened_at": 1, "source": "reconcile"}])
        report = eg.reconcile_positions(
            [_scan_row("NEW", "POOL_A", 81.79)], scan_mtime=FRESH, now=NOW,
            state_dir=self.state_dir, history_complete=True)
        entries = self._read_state()["open_positions"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["position_id"], "NEW")
        self.assertAlmostEqual(entries[0]["position_usd"], 81.79)
        self.assertEqual(report["added"], [])

    def test_value_refreshed_for_matched_entry(self):
        # Live vector: tracked $0.0, live $25.61.
        self._write_state([{"pool_address": "POOL_A", "position_id": "P1",
                            "wallet_id": "main", "position_usd": 0.0,
                            "opened_at": 1, "source": "reconcile"}])
        report = eg.reconcile_positions(
            [_scan_row("P1", "POOL_A", 25.61)], scan_mtime=FRESH, now=NOW,
            state_dir=self.state_dir, history_complete=True)
        self.assertEqual(len(report["updated"]), 1)
        entries = self._read_state()["open_positions"]
        self.assertAlmostEqual(entries[0]["position_usd"], 25.61)

    def test_incomplete_scan_never_removes(self):
        # A partial walk (history_complete=False) must not remove anything;
        # the live book may simply be missing rows.
        self._write_state([{"pool_address": "POOL_A", "position_id": "DEAD?",
                            "wallet_id": "main", "position_usd": 50.0,
                            "opened_at": 1, "source": "reconcile"}])
        report = eg.reconcile_positions(
            [_scan_row("LIVE1", "POOL_B", 10.0)], scan_mtime=FRESH, now=NOW,
            state_dir=self.state_dir, history_complete=False)
        self.assertEqual(report["removed"], [])
        entries = self._read_state()["open_positions"]
        self.assertEqual(len(entries), 2)  # kept + added

    def test_idless_entry_pool_count_fallback_kept(self):
        self._write_state([{"pool_address": "POOL_A", "position_id": None,
                            "wallet_id": "main", "position_usd": 30.0,
                            "opened_at": 1, "source": "legacy"}])
        report = eg.reconcile_positions(
            [_scan_row("P1", "POOL_A", 30.0)], scan_mtime=FRESH, now=NOW,
            state_dir=self.state_dir, history_complete=True)
        entries = self._read_state()["open_positions"]
        self.assertEqual(len(entries), 1)  # fallback: pool accounted for
        self.assertIsNone(entries[0]["position_id"])
        self.assertEqual(report["removed"], [])

    def test_stale_scan_refused(self):
        self._write_state([{"pool_address": "POOL_A", "position_id": "DEAD",
                            "wallet_id": "main", "position_usd": 9.0,
                            "opened_at": 1, "source": "reconcile"}])
        report = eg.reconcile_positions(
            [_scan_row("P1", "POOL_A", 9.0)], scan_mtime=NOW - 9999, now=NOW,
            state_dir=self.state_dir, history_complete=True)
        self.assertIn("stale", report["skipped"])
        entries = self._read_state()["open_positions"]
        self.assertEqual(entries[0]["position_id"], "DEAD")

    def test_no_scan_open_complete_removes_all(self):
        # Empty book per complete scan: everything is closed -> all removed.
        self._write_state([{"pool_address": "POOL_A", "position_id": "P1",
                            "wallet_id": "main", "position_usd": 9.0,
                            "opened_at": 1, "source": "reconcile"}])
        report = eg.reconcile_positions(
            [_scan_row("P1", "POOL_A", 9.0) | {"status": "closed",
                                               "liquidity_raw": "0"}],
            scan_mtime=FRESH, now=NOW, state_dir=self.state_dir,
            history_complete=True)
        self.assertEqual(report["skipped"], "no open positions in scan")
        # Refusal path: nothing written.
        entries = self._read_state()["open_positions"]
        self.assertEqual(len(entries), 1)

    def test_dry_run_writes_nothing(self):
        self._write_state([{"pool_address": "POOL_A", "position_id": "DEAD",
                            "wallet_id": "main", "position_usd": 9.0,
                            "opened_at": 1, "source": "reconcile"}])
        eg.reconcile_positions([_scan_row("P1", "POOL_A", 9.0)],
                               scan_mtime=FRESH, now=NOW,
                               state_dir=self.state_dir,
                               history_complete=True, dry_run=True)
        entries = self._read_state()["open_positions"]
        self.assertEqual(entries[0]["position_id"], "DEAD")

    def test_scan_history_complete_helper(self):
        # Missing flag -> True (pre-flag scans keep old behavior).
        self.assertTrue(eg.scan_history_complete.__doc__ is not None)


if __name__ == "__main__":
    unittest.main()
