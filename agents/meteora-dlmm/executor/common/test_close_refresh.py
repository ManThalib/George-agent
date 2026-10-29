#!/usr/bin/env python3
"""Tests for the close-triggered refresh chain (plan 03 Change F).

After a successful close/rotate/rebalance, George refreshes Missy's
position scan and wallet scan, re-runs Sheldon, and wakes itself only if
Sheldon wrote new signals. A strict close whose position is still in the
fresh scan must NOT re-enter Sheldon (the close -> scan -> close loop
Change F forbids).
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

CFG = {"rpc_https_url": "https://rpc.test", "wallet_public_key": "WALLET"}


class _FakeProc:
    def __init__(self, returncode=0, stdout="scan written\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CloseRefreshTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pos_dir = os.path.join(self.dir, "position_scans")
        self.wallet_dir = os.path.join(self.dir, "wallet_screens")
        self.journal_dir = os.path.join(self.dir, "journal")
        self.state_path = os.path.join(self.dir, "capital_reentry.json")
        self.kill_path = os.path.join(self.dir, "KILL")
        os.makedirs(self.pos_dir)
        os.makedirs(self.wallet_dir)

        self._orig = {
            "pos": exposure_guard.POSITION_SCAN_DIR,
            "wallet": dispatcher.WALLET_SCAN_DIR,
            "journal": None,
            "state": dispatcher.REENTRY_STATE_PATH,
            "kill": dispatcher.KILL_FILE,
            "run": dispatcher.subprocess.run,
            "sleep": dispatcher.time.sleep,
            "notify": dispatcher._notify_owner,
            "sheldon": dispatcher._run_sheldon_reentry,
            "followup": dispatcher._run_dispatcher_followup,
            "wallet_refresh": dispatcher._run_wallet_refresh,
        }
        exposure_guard.POSITION_SCAN_DIR = self.pos_dir
        dispatcher.WALLET_SCAN_DIR = self.wallet_dir
        dispatcher.REENTRY_STATE_PATH = Path(self.state_path)
        dispatcher.KILL_FILE = Path(self.kill_path)
        dispatcher.time.sleep = lambda *_: None
        self.notifications = []
        dispatcher._notify_owner = lambda text: self.notifications.append(text)

        import common.journal as journal
        self._orig["journal"] = journal.JOURNAL_DIR
        journal.JOURNAL_DIR = Path(self.journal_dir)

        self.script_calls = []
        self.scan_positions = []
        self.sheldon_calls = []

        def fake_run(cmd, **kwargs):
            self.script_calls.append(cmd)
            if cmd[-1] == dispatcher.POSITION_REFRESH_SCRIPT:
                self._write_positions(self.scan_positions)
                return _FakeProc(stdout=f"{self.pos_dir}/position_scan-new.json\n")
            # wallet script: write a fresh wallet scan
            path = os.path.join(self.wallet_dir, f"wallet_screen-{len(self.script_calls)}.json")
            with open(path, "w") as fh:
                json.dump({"assets": [{"mint": dispatcher.USDC_MINT, "amount_raw": 100}]}, fh)
            return _FakeProc(stdout="wallet refreshed\n")

        dispatcher.subprocess.run = fake_run
        dispatcher._run_sheldon_reentry = self._fake_sheldon
        dispatcher._run_dispatcher_followup = mock.MagicMock(return_value="ok: done")

    def tearDown(self):
        exposure_guard.POSITION_SCAN_DIR = self._orig["pos"]
        dispatcher.WALLET_SCAN_DIR = self._orig["wallet"]
        dispatcher.REENTRY_STATE_PATH = self._orig["state"]
        dispatcher.KILL_FILE = self._orig["kill"]
        dispatcher.time.sleep = self._orig["sleep"]
        dispatcher._notify_owner = self._orig["notify"]
        dispatcher._run_sheldon_reentry = self._orig["sheldon"]
        dispatcher._run_dispatcher_followup = self._orig["followup"]
        dispatcher._run_wallet_refresh = self._orig["wallet_refresh"]
        dispatcher.subprocess.run = self._orig["run"]
        import common.journal as journal
        journal.JOURNAL_DIR = self._orig["journal"]
        import common.exposure_guard as eg
        eg.POSITION_SCAN_DIR = self._orig["pos"]
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def _fake_sheldon(self):
        self.sheldon_calls.append(1)
        # _run_sheldon_reentry returns (status, stdout, stderr); the follow-up
        # is triggered by the WAKE_GEORGE marker in either stream.
        return ("ok: 1 signal(s)", "WAKE_GEORGE: 1", "")

    def _seed_scan(self):
        """An old scan so a refresh must produce a newer mtime."""
        path = self._write_positions([])
        os.utime(path, (1000, 1000))  # deterministically in the past

    def _write_positions(self, positions, name=None):
        name = name or f"position_scan-{int(dispatcher.time.time() * 1000 % 10 ** 12)}.json"
        path = os.path.join(self.pos_dir, name)
        with open(path, "w") as fh:
            json.dump({"wallet_id": "main", "positions": positions}, fh)
        return path

    def _close(self, action="close", pool="POOLA", position_id="POS1", wallet_id="main"):
        return {"signal_id": f"sig-{action}-{pool}", "action": action,
                "wallet_id": wallet_id, "pool_address": pool,
                "position_id": position_id}

    def _journal_entries(self):
        entries = []
        for name in sorted(os.listdir(self.journal_dir)):
            with open(os.path.join(self.journal_dir, name)) as fh:
                entries.extend(json.loads(ln) for ln in fh if ln.strip())
        return entries

    # -- tests -----------------------------------------------------------
    def test_close_runs_position_scan_then_wallet_then_sheldon_then_wake(self):
        self._seed_scan()
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        # position script before wallet script
        self.assertEqual(self.script_calls[0][-1], dispatcher.POSITION_REFRESH_SCRIPT)
        self.assertIn("run_wallet.sh", self.script_calls[1][-1])
        self.assertEqual(len(self.sheldon_calls), 1)
        dispatcher._run_dispatcher_followup.assert_called_once_with(0)
        self.assertTrue(any("CLOSE REFRESH" in n for n in self.notifications))

    def test_close_absence_confirmed(self):
        self._seed_scan()
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        entry = [e for e in self._journal_entries() if e.get("decision") == "close_refresh"][0]
        absence = entry["details"]["absence"]
        self.assertTrue(absence["asserted"])
        self.assertTrue(absence["absent"])
        self.assertIn("absent from fresh scan", absence["note"])

    def test_strict_close_still_present_blocks_sheldon(self):
        self._seed_scan()
        # the fresh scan still shows the closed pool -> absence fails
        self.scan_positions = [{"wallet_id": "main", "pool_address": "POOLA",
                                "position_address": "POS1"}]
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(self.sheldon_calls, [])
        dispatcher._run_dispatcher_followup.assert_not_called()
        self.assertTrue(any("CLOSE REFRESH INCOMPLETE" in n for n in self.notifications))
        entry = [e for e in self._journal_entries() if e.get("decision") == "close_refresh"][0]
        self.assertFalse(entry["details"]["absence"]["absent"])
        self.assertIn("skipped", entry["details"]["sheldon"])
        self.assertIn("CLOSE REFRESH INCOMPLETE", " ".join(self.notifications))

    def test_closed_row_in_fresh_scan_does_not_block_absence(self):
        # Bug #4 (live 2026-10-10): the screener keeps listing just-closed
        # positions as status=closed rows; that must NOT fail the absence
        # assertion or withhold Sheldon re-entry.
        self._seed_scan()
        self.scan_positions = [{"wallet_id": "main", "pool_address": "POOLA",
                                "position_address": "POS1",
                                "status": "closed", "liquidity_raw": 0}]
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(len(self.sheldon_calls), 1)
        entry = [e for e in self._journal_entries() if e.get("decision") == "close_refresh"][0]
        self.assertTrue(entry["details"]["absence"]["absent"])

    def test_zero_liquidity_row_does_not_block_absence(self):
        self._seed_scan()
        self.scan_positions = [{"wallet_id": "main", "pool_address": "POOLA",
                                "position_address": "POS1",
                                "status": "active", "liquidity_raw": 0}]
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(len(self.sheldon_calls), 1)

    def test_position_id_gone_pool_reused_is_absent(self):
        self._seed_scan()
        self.scan_positions = [{"wallet_id": "main", "pool_address": "POOLA",
                                "position_address": "NEWPOS"}]
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(len(self.sheldon_calls), 1)

    def test_rotate_same_pool_reopen_proceeds(self):
        self._seed_scan()
        self.scan_positions = [{"wallet_id": "main", "pool_address": "POOLA",
                                "position_address": "NEWPOS"}]
        dispatcher._trigger_close_refresh(CFG, [self._close(action="rotate")])
        self.assertEqual(len(self.sheldon_calls), 1)
        entry = [e for e in self._journal_entries() if e.get("decision") == "close_refresh"][0]
        self.assertFalse(entry["details"]["absence"]["asserted"])

    def test_swap_only_does_not_trigger_close_refresh(self):
        with mock.patch.object(dispatcher, "_trigger_close_refresh") as m:
            dispatcher._capital_epilogue(CFG, [{"signal_id": "s1", "action": "swap"}])
            m.assert_not_called()

    def test_close_in_epilogue_triggers_refresh(self):
        with mock.patch.object(dispatcher, "_trigger_close_refresh") as m, \
                mock.patch.object(dispatcher, "_trigger_capital_refresh"), \
                mock.patch.object(dispatcher.mirror, "check_drift", return_value=[]):
            dispatcher._capital_epilogue(CFG, [self._close()])
            m.assert_called_once()

    def test_remove_and_claim_do_not_trigger_refresh(self):
        with mock.patch.object(dispatcher, "_trigger_close_refresh") as m, \
                mock.patch.object(dispatcher.mirror, "check_drift", return_value=[]):
            dispatcher._capital_epilogue(CFG, [
                {"signal_id": "r1", "action": "remove_liquidity"},
                {"signal_id": "c1", "action": "claim_fees"},
            ])
            m.assert_not_called()

    def test_kill_blocks_close_refresh(self):
        self._seed_scan()
        open(self.kill_path, "w").close()
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(self.script_calls, [])
        self.assertEqual(self.sheldon_calls, [])

    def test_hourly_cap_blocks_close_refresh(self):
        self._seed_scan()
        now = dispatcher.time.time()
        # Owner decision 2026-10-10 (#21): close refresh has its OWN hourly
        # cap (20) separate from the swap/capital re-entry cap (8) — filling
        # the swap cap must NOT block a close refresh.
        with open(self.state_path, "w") as fh:
            json.dump({"close_timestamps": [now - i * 60 for i in range(20)]}, fh)
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(self.script_calls, [])
        self.assertTrue(any("CLOSE REFRESH SKIPPED" in n for n in self.notifications))

    def test_swap_cap_does_not_block_close_refresh(self):
        self._seed_scan()
        now = dispatcher.time.time()
        with open(self.state_path, "w") as fh:
            json.dump({"timestamps": [now - i * 60 for i in range(8)]}, fh)
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        # Separate caps: the exhausted swap cap does not consume the close cap.
        self.assertTrue(self.script_calls)

    def test_depth_cap_blocks_close_refresh(self):
        self._seed_scan()
        with mock.patch.dict(os.environ, {"GEORGE_CAPITAL_DEPTH": "3"}):
            dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(self.script_calls, [])
        self.assertTrue(any("CLOSE CHAIN DEPTH LIMIT" in n for n in self.notifications))

    def test_env_skip_flag(self):
        self._seed_scan()
        with mock.patch.dict(os.environ, {"GEORGE_SKIP_CAPITAL_REFRESH": "1"}):
            dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(self.script_calls, [])

    def test_position_script_failure_blocks_sheldon(self):
        self._seed_scan()

        def failing_run(cmd, **kwargs):
            self.script_calls.append(cmd)
            return _FakeProc(returncode=3, stderr="boom")

        dispatcher.subprocess.run = failing_run
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(self.sheldon_calls, [])
        dispatcher._run_dispatcher_followup.assert_not_called()
        entry = [e for e in self._journal_entries() if e.get("decision") == "close_refresh"][0]
        self.assertIn("FAILED", entry["details"]["position_scan"])

    def test_scan_not_refreshed_blocks_sheldon(self):
        self._seed_scan()

        def stale_run(cmd, **kwargs):
            self.script_calls.append(cmd)
            return _FakeProc(stdout="wrote nothing new\n")

        dispatcher.subprocess.run = stale_run
        dispatcher._trigger_close_refresh(CFG, [self._close()])
        self.assertEqual(self.sheldon_calls, [])
        entry = [e for e in self._journal_entries() if e.get("decision") == "close_refresh"][0]
        self.assertIn("not refreshed", entry["details"]["position_scan"])


if __name__ == "__main__":
    unittest.main()
