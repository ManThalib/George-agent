#!/usr/bin/env python3
"""Tests: the on-chain position id reaches the exposure state.

The bug: run_once/do_approve recorded `load_json(signal_path)` — the request
file, which has no position_id. The DEX handler returns the real id in
details["result"]["position_id"], so every executed open was stored with
`position_id: None`. Consequences: position-id matching fell back to pool
matching, and _check_mutation_cooldown (a no-op on a falsy position_id) was
inert for the whole rail.

Live evidence: journal sheldon-1790981020-3 (raydium open, pool 3ucNos…)
carries result.position_id CXBQbhJHHXJ13N73ji2cjh9v6v6FQzEaBjRRVK8gtgsQ,
while state/exposure_state.json held the same position with position_id null.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dispatcher  # noqa: E402
from common import exposure_guard  # noqa: E402

POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
POSITION_ID = "CXBQbhJHHXJ13N73ji2cjh9v6v6FQzEaBjRRVK8gtgsQ"


def _details(position_id=POSITION_ID, dex="raydium"):
    return {
        "result": {
            "ok": True, "dex": dex, "action": "open",
            "mode": "send", "signature": "sig", "slot": 1,
            "position_id": position_id,
        },
    }


class MergeExecutionResultTests(unittest.TestCase):
    def test_position_id_and_dex_copied_from_result(self):
        signal = {"signal_id": "s1", "action": "open", "pool_address": POOL}
        merged = dispatcher._merge_execution_result(signal, _details())
        self.assertEqual(merged["position_id"], POSITION_ID)
        self.assertEqual(merged["dex"], "raydium")

    def test_existing_signal_fields_win(self):
        signal = {"signal_id": "s1", "action": "open",
                  "position_id": "from-signal", "dex": "orca"}
        merged = dispatcher._merge_execution_result(signal, _details())
        self.assertEqual(merged["position_id"], "from-signal")
        self.assertEqual(merged["dex"], "orca")

    def test_wallet_id_defaults_to_main(self):
        signal = {"signal_id": "s1", "action": "open"}
        merged = dispatcher._merge_execution_result(signal, _details())
        self.assertEqual(merged["wallet_id"], "main")

    def test_wallet_id_preserved(self):
        signal = {"signal_id": "s1", "action": "open", "wallet_id": "mirror1"}
        merged = dispatcher._merge_execution_result(signal, _details())
        self.assertEqual(merged["wallet_id"], "mirror1")

    def test_tolerates_missing_or_odd_details(self):
        signal = {"signal_id": "s1", "action": "close"}
        for details in (None, {}, {"result": None}, {"result": "nope"},
                        {"error": "simulation failed"}):
            merged = dispatcher._merge_execution_result(dict(signal), details)
            self.assertIsNone(merged.get("position_id"))
            self.assertEqual(merged["wallet_id"], "main")

    def test_null_result_position_id_not_written(self):
        signal = {"signal_id": "s1", "action": "close"}
        merged = dispatcher._merge_execution_result(
            signal, _details(position_id=None))
        self.assertIsNone(merged.get("position_id"))


class RecordedStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exposure-merge-")
        self.state_path = Path(self.tmp) / "exposure_state.json"
        self._patch = mock.patch.object(exposure_guard, "STATE_PATH",
                                        self.state_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        with open(self.state_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_open_records_real_position_id(self):
        signal = {"signal_id": "s1", "action": "open",
                  "pool_address": POOL, "position_usd": 91.13}
        dispatcher._safe_record_execution_state(signal, _details())
        entry = self._state()["open_positions"][0]
        self.assertEqual(entry["position_id"], POSITION_ID)
        self.assertEqual(entry["wallet_id"], "main")
        self.assertEqual(entry["pool_address"], POOL)

    def test_open_without_details_still_records(self):
        signal = {"signal_id": "s1", "action": "open",
                  "pool_address": POOL, "position_usd": 91.13}
        dispatcher._safe_record_execution_state(signal)
        self.assertEqual(len(self._state()["open_positions"]), 1)

    def test_close_matches_by_pool_after_merge(self):
        signal = {"signal_id": "s1", "action": "open",
                  "pool_address": POOL, "position_usd": 91.13}
        dispatcher._safe_record_execution_state(signal, _details())
        close = {"signal_id": "s2", "action": "close", "pool_address": POOL,
                 "position_id": POSITION_ID}
        dispatcher._safe_record_execution_state(close, {"result": {"ok": True}})
        self.assertEqual(self._state()["open_positions"], [])

    def test_real_position_id_is_available_for_mutations(self):
        """The recorded id lets later add/remove matching use the id path."""
        signal = {"signal_id": "s1", "action": "open",
                  "pool_address": POOL, "position_usd": 91.13}
        dispatcher._safe_record_execution_state(signal, _details())
        state = exposure_guard.load_state(self.state_path)
        entry = state["open_positions"][0]
        self.assertEqual(entry["position_id"], POSITION_ID)
        self.assertEqual(exposure_guard._find_position(
            state["open_positions"], {"position_id": POSITION_ID}), entry)

    def test_guard_failure_never_raises(self):
        with mock.patch.object(exposure_guard, "record_open_executed",
                               side_effect=RuntimeError("boom")):
            dispatcher._safe_record_execution_state(
                {"signal_id": "s1", "action": "open"}, _details())


class RecordDecisionTests(unittest.TestCase):
    """_record_decision: one journal write, ledger only when capital moved.

    It replaced six copies of the same block. The subtle case is an executed
    open whose self-verify failed: it journals as executed_unverified (so
    downstream consumers do not read it as 'capital deployed and verified') but
    must still produce a ledger row, because the transaction did land.
    """

    def _run(self, decision, ledger=None):
        journaled, ledgered = [], []
        with mock.patch.object(dispatcher, "journal_append",
                               side_effect=lambda **kw: journaled.append(kw)), \
                mock.patch.object(dispatcher.trade_ledger, "append_row",
                                  side_effect=lambda row: ledgered.append(row)):
            dispatcher._record_decision(
                {"signal_id": "s1", "action": "open"}, decision, {"ok": True},
                ledger=ledger)
        return journaled, ledgered

    def test_executed_writes_both(self):
        journaled, ledgered = self._run("executed")
        self.assertEqual(len(journaled), 1)
        self.assertEqual(len(ledgered), 1)

    def test_rejected_writes_journal_only(self):
        journaled, ledgered = self._run("rejected")
        self.assertEqual(len(journaled), 1)
        self.assertEqual(ledgered, [])

    def test_executed_unverified_journals_that_decision_but_still_ledgers(self):
        journaled, ledgered = self._run("executed_unverified", ledger=True)
        self.assertEqual(journaled[0]["decision"], "executed_unverified")
        self.assertEqual(len(ledgered), 1)

    def test_ledger_failure_never_raises(self):
        with mock.patch.object(dispatcher, "journal_append"), \
                mock.patch.object(dispatcher.trade_ledger, "append_row",
                                  side_effect=OSError("disk full")):
            dispatcher._record_decision({"signal_id": "s1"}, "executed", {})

    def test_journal_carries_the_rails_hash(self):
        journaled, _ = self._run("rejected")
        self.assertIsInstance(journaled[0]["rails_hash"], str)


if __name__ == "__main__":
    unittest.main()
