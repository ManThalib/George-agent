#!/usr/bin/env python3
"""The chain-request envelope: one slippage clamp, one fee cap.

Five builders used to spell the envelope out independently, each with its own
copy of the slippage clamp. These tests pin the shared behaviour, so a future
sixth builder cannot quietly invent a looser one.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.chain_request import base_request, claim_action, slippage_bps  # noqa: E402
from dexes.meteora import builder as meteora_builder  # noqa: E402
from dexes.orca import builder as orca_builder  # noqa: E402
from dexes.raydium import builder as raydium_builder  # noqa: E402

CFG = {
    "rpc_https_url": "https://rpc.test",
    "rpc_fallback_urls": ["https://fallback.test"],
    "rpc_fallback_delay_seconds": 15,
    "wallet_public_key": "WALLETKEY1111111111111111111111111111111111",
}
RAILS = {"max_slippage_bps": 100, "priority_fee_cap_sol": 0.0005,
         "max_price_impact_pct": 1.5, "claim_min_usd": 1.0}


def open_signal(**extra):
    signal = {
        "signal_id": "req-1",
        "action": "open",
        "dex": "meteora",
        "pool_address": "POOL",
        "bin_range": {"lower": -10, "upper": 10},
        "liquidity": {"amount_x": "1", "amount_y": "2"},
        "position_usd": 50.0,
        "max_slippage_bps": 50,
    }
    signal.update(extra)
    return signal


class SlippageClampTests(unittest.TestCase):
    def test_signal_below_rail_is_honoured(self):
        self.assertEqual(slippage_bps({"max_slippage_bps": 50}, RAILS), 50)

    def test_signal_above_rail_is_clamped(self):
        self.assertEqual(slippage_bps({"max_slippage_bps": 500}, RAILS), 100)

    def test_missing_signal_value_uses_the_rail(self):
        self.assertEqual(slippage_bps({}, RAILS), 100)

    def test_garbage_signal_value_uses_the_rail(self):
        for bad in ("nonsense", None, {}, []):
            with self.subTest(value=bad):
                self.assertEqual(slippage_bps({"max_slippage_bps": bad}, RAILS), 100)

    def test_missing_rail_uses_the_built_in_ceiling(self):
        self.assertEqual(slippage_bps({"max_slippage_bps": 500}, {}), 100)


class BuilderEnvelopeTests(unittest.TestCase):
    """Every DEX builder must emit the same envelope for the same inputs."""

    def test_all_three_builders_share_the_envelope(self):
        for dex, builder in (("meteora", meteora_builder),
                             ("orca", orca_builder),
                             ("raydium", raydium_builder)):
            with self.subTest(dex=dex):
                req = builder.build(open_signal(dex=dex, max_slippage_bps=999), CFG, RAILS)
                self.assertEqual(req["dex"], dex)
                self.assertEqual(req["action"], "open")
                self.assertEqual(req["mode"], "send")
                self.assertEqual(req["rpc_url"], CFG["rpc_https_url"])
                self.assertEqual(req["fallback_rpc_urls"], CFG["rpc_fallback_urls"])
                self.assertEqual(req["wallet_public_key"], CFG["wallet_public_key"])
                self.assertEqual(req["wallet_id"], "main")
                self.assertEqual(req["max_slippage_bps"], 100)  # clamped
                self.assertEqual(req["pool_address"], "POOL")

    def test_wallet_id_is_carried_for_mirror_legs(self):
        req = orca_builder.build(open_signal(dex="orca", wallet_id="w2"), CFG, RAILS)
        self.assertEqual(req["wallet_id"], "w2")

    def test_claim_actions_map_to_claim(self):
        for raw in ("claim_fees", "claim_rewards"):
            with self.subTest(action=raw):
                self.assertEqual(claim_action(raw), "claim")
        self.assertEqual(claim_action("open"), "open")

    def test_missing_position_id_is_rejected_by_every_builder(self):
        for dex, builder in (("meteora", meteora_builder),
                             ("orca", orca_builder),
                             ("raydium", raydium_builder)):
            with self.subTest(dex=dex):
                with self.assertRaises(ValueError):
                    builder.build({"action": "close", "pool_address": "POOL"}, CFG, RAILS)

    def test_raydium_carries_the_claim_floor(self):
        req = raydium_builder.build(
            {"action": "claim_fees", "pool_address": "POOL",
             "position_id": "POS", "wallet_id": "main"}, CFG, RAILS)
        self.assertEqual(req["claim_min_usd"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
