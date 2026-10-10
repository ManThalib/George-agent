#!/usr/bin/env python3
"""Tests for the explicit signal priority sorter (plan 03 Change G).

Pending signals are processed in priority order, not filename order:
close > rotate/rebalance/remove > SOL top-up prep > target-token prep >
open > dust (swap_to_usdc) > claims. A backlog of stale preps must never
delay a close.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dispatcher  # noqa: E402

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = dispatcher.WSOL_MINT
TARGET = "A7bdiYdSCt9GCKyu3r9DdZxDloGVGGzMy2PnjlRAaGq"


class PrioritySorterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pending = os.path.join(self.dir, "pending")
        os.makedirs(self.pending)
        self._orig_signals = dispatcher.SIGNALS_DIR
        dispatcher.SIGNALS_DIR = Path(self.dir)

    def tearDown(self):
        dispatcher.SIGNALS_DIR = self._orig_signals
        shutil.rmtree(self.dir, ignore_errors=True)

    def _sig(self, name, **fields):
        path = os.path.join(self.pending, name)
        with open(path, "w") as fh:
            json.dump(fields, fh)
        return path

    def _order(self):
        return [p.name for p in dispatcher._iter_pending_by_priority()]

    def test_full_ranking_across_all_classes(self):
        # Names in REVERSE alphabetical so filename order would sort them
        # opposite to the required priority order.
        self._sig("zz-claim_fees.json", action="claim_fees")
        self._sig("yy-swap_to_usdc.json", action="swap_to_usdc", mint=TARGET)
        self._sig("mm-open.json", action="open")
        self._sig("cc-prep-token.json", action="swap", input_mint=USDC,
                  output_mint=TARGET)
        self._sig("bb-prep-sol.json", action="swap", input_mint=USDC,
                  output_mint=WSOL)
        self._sig("b2-remove.json", action="remove_liquidity")
        self._sig("a1-close.json", action="close")
        self.assertEqual(self._order(), [
            "a1-close.json",
            "b2-remove.json",
            "bb-prep-sol.json",
            "cc-prep-token.json",
            "mm-open.json",
            "yy-swap_to_usdc.json",
            "zz-claim_fees.json",
        ])

    def test_close_outranks_swap_and_open(self):
        self._sig("s-swap.json", action="swap", input_mint=USDC, output_mint=TARGET)
        self._sig("o-open.json", action="open")
        self._sig("c-close.json", action="close")
        # plan ranks: close(1) > swap(4) > open(5)
        self.assertEqual(self._order(), ["c-close.json", "s-swap.json", "o-open.json"])

    def test_sol_topup_prep_outranks_target_token_prep(self):
        self._sig("p1-token.json", action="swap", input_mint=USDC, output_mint=TARGET)
        self._sig("p2-sol.json", action="swap", input_mint=USDC, output_mint=WSOL)
        self.assertEqual(self._order(), ["p2-sol.json", "p1-token.json"])

    def test_swap_outranks_open_regardless_of_prefix(self):
        self._sig("x-swap.json", action="swap", input_mint=USDC, output_mint=TARGET)
        self._sig("a-other.json", action="open")
        # plan ranks: swap(4) > open(5)
        self.assertEqual(self._order(), ["x-swap.json", "a-other.json"])

    def test_wsol_swap_is_rank_three_even_without_prep_prefix(self):
        self._sig("x-sol.json", action="swap", input_mint=USDC, output_mint=WSOL)
        self._sig("a-open.json", action="open")
        self._sig("z-token-prep.json", action="swap", input_mint=USDC, output_mint=TARGET)
        self.assertEqual(self._order(), ["x-sol.json", "z-token-prep.json", "a-open.json"])

    def test_rotate_rebalance_remove_share_rank_two(self):
        self._sig("r-rotate.json", action="rotate")
        self._sig("b-rebalance.json", action="rebalance")
        self._sig("m-remove.json", action="remove_liquidity")
        self._sig("c-close.json", action="close")
        self._sig("o-open.json", action="open")
        self.assertEqual(self._order()[0], "c-close.json")
        self.assertEqual(sorted(self._order()[1:4]),
                         ["b-rebalance.json", "m-remove.json", "r-rotate.json"])
        self.assertEqual(self._order()[4], "o-open.json")

    def test_claims_and_dust_rank_below_open(self):
        self._sig("a-claim1.json", action="claim_fees")
        self._sig("b-claim2.json", action="claim_rewards")
        self._sig("c-dust.json", action="swap_to_usdc", mint=TARGET)
        self._sig("d-open.json", action="open")
        self.assertEqual(self._order(),
                         ["d-open.json", "c-dust.json", "a-claim1.json", "b-claim2.json"])

    def test_unreadable_signal_takes_neutral_rank(self):
        self._sig("z-close.json", action="close")
        with open(os.path.join(self.pending, "m-broken.json"), "w") as fh:
            fh.write("{not json")
        self._sig("a-open.json", action="open")
        self.assertEqual(self._order(), ["z-close.json", "a-open.json", "m-broken.json"])

    def test_unknown_action_takes_neutral_rank(self):
        self._sig("weird.json", action="teleport")
        self._sig("c-close.json", action="close")
        self.assertEqual(self._order(), ["c-close.json", "weird.json"])

    def test_empty_pending(self):
        self.assertEqual(self._order(), [])

    def test_ties_break_by_filename(self):
        for name in ("d-open.json", "c-open.json", "b-open.json", "a-open.json"):
            self._sig(name, action="open")
        self.assertEqual(self._order(),
                         ["a-open.json", "b-open.json", "c-open.json", "d-open.json"])


if __name__ == "__main__":
    unittest.main()
