#!/usr/bin/env python3
"""Tests for the capital-refresh USDC delta verification.

The bug: _read_latest_usdc_raw picked the scan by filename, so it always
resolved wallet_screen-latest.json. When a refresh wrote a new timestamped
file but the symlink lagged, pre and cur were the same scan and the loop
reported a false "delta unverified".
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

import dispatcher  # noqa: E402
from common import exposure_guard  # noqa: E402

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CFG = {"rpc_https_url": "https://rpc.test", "wallet_public_key": "WALLET"}


class _FakeProc:
    def __init__(self, returncode=0, stdout="refreshed\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class UsdcDeltaTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig_dir = dispatcher.WALLET_SCAN_DIR
        self._orig_sleep = dispatcher.time.sleep
        dispatcher.WALLET_SCAN_DIR = self.dir
        dispatcher.time.sleep = lambda *_: None  # no 6s waits

    def tearDown(self):
        dispatcher.WALLET_SCAN_DIR = self._orig_dir
        dispatcher.time.sleep = self._orig_sleep
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_scan(self, name, usdc_raw, mtime):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            json.dump({"assets": [{"mint": USDC, "amount_raw": usdc_raw}]}, fh)
        os.utime(path, (mtime, mtime))
        return path

    def test_read_picks_newest_by_mtime(self):
        self._write_scan("wallet_screen-latest.json", 100, 1000)
        self._write_scan("wallet_screen-20261006-000000.json", 250, 2000)
        raw, path, mtime = dispatcher._read_usdc_scan()
        self.assertEqual(raw, 250)
        self.assertTrue(path.endswith("wallet_screen-20261006-000000.json"))

    def test_missing_config_skips(self):
        out = dispatcher._run_wallet_refresh({}, 0, False)
        self.assertIn("skipped", out)

    def test_unchanged_scan_reports_not_refreshed(self):
        # Refresh runs but never writes a new scan -> must NOT say "unverified".
        self._write_scan("wallet_screen-latest.json", 100, 1000)
        with mock.patch.object(dispatcher.subprocess, "run",
                               return_value=_FakeProc()):
            out = dispatcher._run_wallet_refresh(CFG, -5_000_000, False)
        self.assertIn("scan not refreshed", out)
        self.assertNotIn("delta unverified", out)

    def test_new_scan_with_expected_delta_reports_ok(self):
        self._write_scan("wallet_screen-latest.json", 10_000_000, 1000)

        def fake_run(*a, **k):
            # The refresh lands a newer scan with less USDC (a buy).
            self._write_scan("wallet_screen-20261006-010000.json", 5_000_000, 3000)
            return _FakeProc()

        with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
            out = dispatcher._run_wallet_refresh(CFG, -5_000_000, False)
        self.assertIn("ok (usdc 10000000 -> 5000000)", out)

    def test_ambiguous_batch_does_not_assert_delta(self):
        self._write_scan("wallet_screen-latest.json", 10_000_000, 1000)

        def fake_run(*a, **k):
            # A buy+sell batch: USDC barely moved, but a new scan did land.
            self._write_scan("wallet_screen-20261006-020000.json", 10_000_100, 3000)
            return _FakeProc()

        with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
            out = dispatcher._run_wallet_refresh(CFG, -5_000_000, True)
        self.assertIn("ambiguous batch, delta not asserted", out)
        self.assertNotIn("delta unverified", out)

    def test_zero_expected_delta_reports_ok(self):
        self._write_scan("wallet_screen-latest.json", 1_000_000, 1000)

        def fake_run(*a, **k):
            self._write_scan("wallet_screen-20261006-030000.json", 1_000_000, 3000)
            return _FakeProc()

        with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
            out = dispatcher._run_wallet_refresh(CFG, 0, False)
        self.assertTrue(out.startswith("ok"))


if __name__ == "__main__":
    unittest.main()
