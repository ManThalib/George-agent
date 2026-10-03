#!/usr/bin/env python3
"""Tests for the raydium claim gate: builder rail + dispatcher skip routing."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dexes.raydium import builder as raydium_builder
from common import rails_loader
import dispatcher


CFG = {
    "rpc_https_url": "https://rpc.example",
    "rpc_fallback_urls": [],
    "rpc_fallback_delay_seconds": 15,
    "wallet_public_key": "QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj",
    "mode": "auto",
}

import time
SIGNAL = {
    "signal_id": "test-claim-1",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "action": "claim_fees",
    "dex": "raydium",
    "pool_address": "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv",
    "position_id": "9X3StbggkAiqwMDmYgGynwkAKcsY96AZbgnpNLEnJt1U",
}


class ClaimRailTests(unittest.TestCase):
    def test_builder_includes_claim_min_usd_from_rails(self):
        req = raydium_builder.build(SIGNAL, CFG, {"claim_min_usd": 2.5})
        self.assertEqual(req["claim_min_usd"], 2.5)

    def test_builder_defaults_to_zero_without_rail(self):
        req = raydium_builder.build(SIGNAL, CFG, {})
        self.assertEqual(req["claim_min_usd"], 0)

    def test_execution_limits_rail_present(self):
        rails = rails_loader.load_rails()
        self.assertIn("claim_min_usd", rails)
        self.assertGreater(float(rails["claim_min_usd"]), 0)


class DispatchSkipTests(unittest.TestCase):
    """A skipped (under-threshold) claim is not an error and not retried."""

    def _auto_send_result(self, result):
        sim_ok = {"ok": True, "tx_base64": ["x"], "simulation": {"ok": True}}
        with mock.patch.object(dispatcher, "chain_simulate", return_value=sim_ok), \
             mock.patch.object(dispatcher, "chain_send", return_value=result):
            return dispatcher.process_signal(dict(SIGNAL), dict(CFG), {"claim_min_usd": 1.0})

    def test_skipped_claim_reports_skip_with_pending(self):
        result = {
            "skipped": True,
            "skip_reason": "claimable $0.0154 < claim_min_usd 1.0",
            "pending_fees_raw": ["61077", "8550"],
            "pending_rewards_raw": ["0", "0", "0"],
            "pending_usd": {"fees_usd": 0.0154, "rewards_usd": 0.0, "total_usd": 0.0154},
        }
        status, details = self._auto_send_result(result)
        self.assertEqual(status, "skipped")
        self.assertEqual(details["skip_reason"], result["skip_reason"])
        self.assertEqual(details["pending_usd"], 0.0154)

    def test_normal_claim_still_executes(self):
        result = {
            "signatures": [{"signature": "5x", "slot": 1, "confirmed_via": "confirmed"}],
            "pending_fees_raw": ["1000000", "0"],
            "pending_rewards_raw": ["0", "0", "0"],
            "pending_usd": {"fees_usd": 5.0, "rewards_usd": 0.0, "total_usd": 5.0},
        }
        with mock.patch.object(dispatcher, "chain_simulate", return_value={"ok": True, "simulation": {"ok": True}}), \
             mock.patch.object(dispatcher, "chain_send", return_value=result):
            # _handle_auto calls _record_execution_state after send; claim maps there
            status, details = dispatcher._handle_auto(dict(SIGNAL), dict(CFG), {"claim_min_usd": 1.0})
        self.assertEqual(status, "executed")
        self.assertNotIn("skip_reason", details)


if __name__ == "__main__":
    unittest.main()


class MirrorLegTests(unittest.TestCase):
    """Mirror expansion: structural copy, gated opens, drift records."""

    REGISTRY = {
        "main": "QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj",
        "w2": "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T",
    }

    OPEN_SIGNAL = {
        "signal_id": "sheldon-1-0",
        "created_at": "2026-10-03T00:00:00Z",
        "action": "open",
        "dex": "raydium",
        "wallet_id": "main",
        "pool_address": "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv",
        "bin_range": {"lower": -22044, "upper": -20640},
        "liquidity": {"amount_x": "1000000000", "amount_y": "5000000"},
        "position_usd": 100.0,
        "score": 80.0,
        "max_slippage_bps": 100,
    }

    def test_build_mirror_scales_capital(self):
        from common import mirror
        leg = mirror.build_mirror_signal(self.OPEN_SIGNAL, "w2", 1.0)
        self.assertEqual(leg["wallet_id"], "w2")
        self.assertEqual(leg["signal_id"], "sheldon-1-0:w2")
        self.assertEqual(leg["liquidity"]["amount_x"], "1000000000")
        leg_half = mirror.build_mirror_signal(self.OPEN_SIGNAL, "w2", 0.5)
        self.assertEqual(leg_half["liquidity"]["amount_x"], "500000000")
        self.assertEqual(leg_half["position_usd"], 50.0)

    def test_swap_not_mirrorable(self):
        from common import mirror
        sig = dict(self.OPEN_SIGNAL, action="swap")
        self.assertIsNone(mirror.build_mirror_signal(sig, "w2"))

    def test_mirror_ids_exclude_main(self):
        from common import mirror
        self.assertEqual(mirror.mirror_wallet_ids(self.REGISTRY), ["w2"])

    def test_mirror_open_gated_when_main_fails(self):
        import dispatcher
        from common import mirror as mirror_mod
        cfg = {"wallet_registry": self.REGISTRY, "rpc_https_url": "x",
               "rpc_fallback_urls": [], "rpc_fallback_delay_seconds": 1,
               "wallet_public_key": self.REGISTRY["main"]}
        calls = []
        with mock.patch.object(dispatcher, "chain_send",
                               side_effect=lambda req: calls.append(req) or (_ for _ in ()).throw(RuntimeError("boom"))):
            legs = dispatcher._run_mirror_legs(dict(self.OPEN_SIGNAL), "main",
                                               main_status="failed", cfg=cfg, rails={})
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["status"], "gated")
        self.assertEqual(calls, [])  # gated: no chain call
        drift = mirror_mod.check_drift()
        self.assertTrue(any(d["wallet_id"] == "w2" for d in drift))
        mirror_mod.clear_drift(signal_id="sheldon-1-0")

    def test_mirror_close_runs_even_when_main_failed(self):
        import dispatcher
        from common import mirror as mirror_mod
        sig = dict(self.OPEN_SIGNAL, action="close", position_id="9X3StbggkAiqwMDmYgGynwkAKcsY96AZbgnpNLEnJt1U")
        cfg = {"wallet_registry": self.REGISTRY, "rpc_https_url": "x",
               "rpc_fallback_urls": [], "rpc_fallback_delay_seconds": 1,
               "wallet_public_key": self.REGISTRY["main"]}
        sent = []
        with mock.patch.object(dispatcher, "chain_send",
                               side_effect=lambda req: sent.append(req) or {"signatures": []}):
            legs = dispatcher._run_mirror_legs(sig, "main",
                                               main_status="failed", cfg=cfg, rails={})
        self.assertEqual(len(sent), 1)  # close ran despite main failure
        self.assertEqual(legs[0]["status"], "executed")
        self.assertEqual(sent[0]["wallet_id"], "w2")
        mirror_mod.clear_drift(signal_id=sig["signal_id"])
