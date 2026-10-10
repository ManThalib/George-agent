#!/usr/bin/env python3
"""Tests for the live SOL/token preflight on opens (George plan 03, B-D)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import live_preflight as lp
import dispatcher


USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_B = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"

CFG = {"rpc_https_url": "https://rpc.example",
       "wallet_public_key": "QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj"}

RAILS = {
    "sol_overhead_lamports": {"meteora": 45_000_000, "orca": 30_000_000,
                              "raydium": 30_000_000, "default": 45_000_000},
    "ata_rent_lamports": 2_039_280,
    "base_fee_lamports": 5000,
    "tx_signatures": 2,
    "sol_reserve_lamports": 5_000_000,
    "priority_fee_cap_sol": 0.0005,
}


def make_signal(dex="meteora", amount_x="0", amount_y="0", **extra):
    signal = {
        "signal_id": "test-open-1",
        "action": "open",
        "dex": dex,
        "pool_address": "POOLADDRESS111111111111111111111111111111",
        "bin_range": {"lower": -10, "upper": 10},
        "liquidity": {"amount_x": amount_x, "amount_y": amount_y},
        "position_usd": 50.0,
        "score": 90.0,
    }
    signal.update(extra)
    return signal


def fetchers(native, tokens, mints):
    return (lambda *a, **k: native,
            lambda *a, **k: dict(tokens),
            lambda *a, **k: tuple(mints))


class SolPreflightTests(unittest.TestCase):
    def test_insufficient_sol_rejected_with_mechanical_code(self):
        sol, tok, mints = fetchers(44_263_180, {}, (USDC, TOKEN_B))
        ok, details = lp.preflight_open(
            make_signal(amount_x="6307000", amount_y="750053"),
            CFG, RAILS, sol_fetcher=sol, tokens_fetcher=tok, mints_fetcher=mints)
        self.assertFalse(ok)
        self.assertEqual(details["stage"], "preflight_sol")
        self.assertEqual(details["error_code"], "INSUFFICIENT_SOL_FOR_OPEN")
        self.assertEqual(details["available_lamports"], 44_263_180)
        self.assertEqual(details["shortfall_lamports"],
                         details["required_lamports"] - 44_263_180)
        self.assertGreater(details["shortfall_lamports"], 0)
        self.assertIn("breakdown", details)

    def test_sufficient_sol_passes_when_tokens_funded(self):
        sol, tok, mints = fetchers(
            1_000_000_000, {USDC: 10_000_000, TOKEN_B: 10_000_000},
            (USDC, TOKEN_B))
        ok, details = lp.preflight_open(
            make_signal(amount_x="6307000", amount_y="750053"),
            CFG, RAILS, sol_fetcher=sol, tokens_fetcher=tok, mints_fetcher=mints)
        self.assertTrue(ok)
        self.assertIsNone(details)

    def test_missing_ata_rent_is_charged(self):
        # No token account exists yet -> rent for two ATAs is required.
        sol, tok, mints = fetchers(1_000_000_000, {}, (USDC, TOKEN_B))
        ok, details = lp.preflight_open(make_signal(amount_x="0", amount_y="0"),
                                        CFG, RAILS, sol_fetcher=sol,
                                        tokens_fetcher=tok, mints_fetcher=mints)
        self.assertTrue(ok)
        # Use a tight balance to observe the rent contribution.
        sol2, tok2, mints2 = fetchers(32_100_000, {}, (USDC, TOKEN_B))
        ok2, details2 = lp.preflight_open(make_signal(amount_x="0", amount_y="0"),
                                          CFG, RAILS, sol_fetcher=sol2,
                                          tokens_fetcher=tok2, mints_fetcher=mints2)
        self.assertFalse(ok2)
        self.assertEqual(details2["breakdown"]["ata_rent_lamports"],
                         2 * 2_039_280)

    def test_existing_token_account_charges_no_rent(self):
        sol, tok, mints = fetchers(1_000_000_000, {}, (USDC, TOKEN_B))
        ok, details = lp.preflight_open(make_signal(amount_x="1", amount_y="1"),
                                        CFG, RAILS, sol_fetcher=sol,
                                        tokens_fetcher=lambda *a: {USDC: 1, TOKEN_B: 1},
                                        mints_fetcher=mints)
        self.assertTrue(ok)


class TokenPreflightTests(unittest.TestCase):
    def test_insufficient_token_x_rejected(self):
        sol, tok, mints = fetchers(1_000_000_000, {TOKEN_B: 10_000_000},
                                   (USDC, TOKEN_B))
        ok, details = lp.preflight_open(
            make_signal(amount_x="6307000", amount_y="750053"),
            CFG, RAILS, sol_fetcher=sol, tokens_fetcher=tok, mints_fetcher=mints)
        self.assertFalse(ok)
        self.assertEqual(details["stage"], "preflight_tokens")
        self.assertEqual(details["error_code"], "INSUFFICIENT_TOKEN_BALANCE")
        self.assertEqual(details["token_x"]["required_raw"], "6307000")
        self.assertEqual(details["token_x"]["available_raw"], "0")
        self.assertEqual(details["shortfall_x_raw"], "6307000")
        self.assertEqual(details["token_y"]["available_raw"], "10000000")

    def test_insufficient_token_y_rejected(self):
        sol, tok, mints = fetchers(1_000_000_000, {USDC: 10_000_000},
                                   (USDC, TOKEN_B))
        ok, details = lp.preflight_open(
            make_signal(amount_x="100000", amount_y="750053"),
            CFG, RAILS, sol_fetcher=sol, tokens_fetcher=tok, mints_fetcher=mints)
        self.assertFalse(ok)
        self.assertEqual(details["error_code"], "INSUFFICIENT_TOKEN_BALANCE")
        self.assertEqual(details["shortfall_y_raw"], "750053")

    def test_token_shortfall_is_position_size_aware(self):
        small, _, _ = fetchers(1_000_000_000, {USDC: 1_000_000, TOKEN_B: 1_000_000},
                               (USDC, TOKEN_B))
        ok_small, _ = lp.preflight_open(make_signal(amount_x="100", amount_y="100"),
                                        CFG, RAILS, sol_fetcher=small,
                                        tokens_fetcher=lambda *a: {USDC: 1_000_000, TOKEN_B: 1_000_000},
                                        mints_fetcher=lambda *a: (USDC, TOKEN_B))
        # larger amounts exceed the live balances
        sol, tok, mints = fetchers(1_000_000_000, {USDC: 1_000_000, TOKEN_B: 1_000_000},
                                   (USDC, TOKEN_B))
        ok_big, details = lp.preflight_open(
            make_signal(amount_x="5000000", amount_y="100"),
            CFG, RAILS, sol_fetcher=sol, tokens_fetcher=tok, mints_fetcher=mints)
        self.assertTrue(ok_small)
        self.assertFalse(ok_big)
        self.assertEqual(details["error_code"], "INSUFFICIENT_TOKEN_BALANCE")


class SolPairAwareTests(unittest.TestCase):
    def test_wsol_side_funded_from_native_sol(self):
        # WSOL on the X side: no SPL WSOL account needed; native SOL covers it.
        sol, tok, mints = fetchers(60_000_000, {USDC: 1},
                                   (WSOL, USDC))
        ok, details = lp.preflight_open(make_signal(amount_x="40000000", amount_y="0"),
                                        CFG, RAILS, sol_fetcher=sol,
                                        tokens_fetcher=tok, mints_fetcher=mints)
        self.assertFalse(ok)
        self.assertEqual(details["error_code"], "INSUFFICIENT_SOL_FOR_OPEN")
        self.assertEqual(details["sol_side"], "x")
        self.assertGreater(details["breakdown"]["sol_topup_lamports"], 0)

    def test_wsol_balance_offsets_native_topup(self):
        # 40M WSOL raw deposit; 30M already wrapped means 10M native topup.
        sol, tok, mints = fetchers(
            100_000_000, {WSOL: 30_000_000, USDC: 1}, (WSOL, USDC))
        ok, details = lp.preflight_open(make_signal(amount_x="40000000", amount_y="0"),
                                        CFG, RAILS, sol_fetcher=sol,
                                        tokens_fetcher=tok, mints_fetcher=mints)
        self.assertTrue(ok)
        # A tighter native balance fails; the wrapped 30M still counts.
        sol2, tok2, mints2 = fetchers(
            44_000_000, {WSOL: 30_000_000, USDC: 1}, (WSOL, USDC))
        ok2, details2 = lp.preflight_open(
            make_signal(amount_x="40000000", amount_y="0"), CFG, RAILS,
            sol_fetcher=sol2, tokens_fetcher=tok2, mints_fetcher=mints2)
        self.assertFalse(ok2)
        self.assertEqual(details2["breakdown"]["wsol_balance_raw"], "30000000")

    def test_declared_mints_skip_pool_fetch(self):
        def boom(*a, **k):
            raise AssertionError("pool fetch should be skipped")
        sig = make_signal(amount_x="100", amount_y="100",
                          mint_x=USDC, mint_y=TOKEN_B)
        ok, _ = lp.preflight_open(
            sig, CFG, RAILS,
            sol_fetcher=lambda *a: 1_000_000_000,
            tokens_fetcher=lambda *a: {USDC: 1000, TOKEN_B: 1000},
            mints_fetcher=boom)
        self.assertTrue(ok)


class DexSpecificTests(unittest.TestCase):
    def test_meteora_overhead_exceeds_orca(self):
        # Same balances, same liquidity: Meteora's bin-array rent makes the
        # requirement higher than Orca's.
        native = 45_000_000
        sol, tok, mints = fetchers(native, {USDC: 0, TOKEN_B: 0}, (USDC, TOKEN_B))

        ok_meta, details_meta = lp.preflight_open(
            make_signal(dex="meteora", amount_x="0", amount_y="0"),
            CFG, RAILS, sol_fetcher=sol, tokens_fetcher=tok, mints_fetcher=mints)
        ok_orca, _ = lp.preflight_open(
            make_signal(dex="orca", amount_x="0", amount_y="0"),
            CFG, RAILS, sol_fetcher=sol, tokens_fetcher=tok, mints_fetcher=mints)
        self.assertFalse(ok_meta)
        self.assertTrue(ok_orca)
        self.assertGreater(details_meta["breakdown"]["overhead_lamports"], 0)


class FailClosedTests(unittest.TestCase):
    def test_sol_read_failure_fails_closed(self):
        def boom(*a, **k):
            raise RuntimeError("rpc down")
        ok, details = lp.preflight_open(
            make_signal(amount_x="1", amount_y="1"), CFG, RAILS,
            sol_fetcher=boom, tokens_fetcher=lambda *a: {}, mints_fetcher=lambda *a: (USDC, TOKEN_B))
        self.assertFalse(ok)
        self.assertEqual(details["error_code"], "PREFLIGHT_UNAVAILABLE")

    def test_pool_mints_unreadable_fails_closed(self):
        ok, details = lp.preflight_open(
            make_signal(amount_x="1", amount_y="1"), CFG, RAILS,
            sol_fetcher=lambda *a: 1_000_000_000,
            tokens_fetcher=lambda *a: {},
            mints_fetcher=lambda *a: None)
        self.assertFalse(ok)
        self.assertEqual(details["error_code"], "PREFLIGHT_POOL_UNREADABLE")


class DispatcherGateTests(unittest.TestCase):
    """The preflight must run before simulation and block signing on failure."""

    def _run(self, preflight_result):
        signal = make_signal(amount_x="100", amount_y="100")
        sim_calls = []
        send_calls = []

        class FakeBuilder:
            @staticmethod
            def build(sig, cfg, rails):
                return {"dex": "meteora", "action": "open", "mode": "send",
                        "pool_address": sig["pool_address"]}

        with mock.patch.object(dispatcher, "live_preflight") as lp_mod, \
             mock.patch.object(dispatcher, "_get_dex_module",
                               return_value=(lambda s, c, r: s, FakeBuilder)), \
             mock.patch.object(dispatcher.exposure_guard, "check_open_allowed",
                               return_value=(True, "")), \
             mock.patch.object(dispatcher, "chain_simulate",
                               side_effect=lambda req: sim_calls.append(req) or {"ok": True, "simulation": {"ok": True}}), \
             mock.patch.object(dispatcher, "chain_send",
                               side_effect=lambda req: send_calls.append(req) or {"signature": "x"}):
            lp_mod.preflight_open.return_value = preflight_result
            status, details = dispatcher.process_signal(
                dict(signal), dict(CFG, mode="auto"), dict(RAILS))
        return status, details, sim_calls, send_calls

    def test_failed_preflight_blocks_simulation_and_signing(self):
        reject = {"stage": "preflight_sol",
                  "error_code": "INSUFFICIENT_SOL_FOR_OPEN",
                  "required_lamports": 1, "available_lamports": 0,
                  "shortfall_lamports": 1}
        status, details, sim_calls, send_calls = self._run((False, reject))
        self.assertEqual(status, "rejected")
        self.assertEqual(details["error_code"], "INSUFFICIENT_SOL_FOR_OPEN")
        self.assertEqual(sim_calls, [])
        self.assertEqual(send_calls, [])

    def test_passing_preflight_reaches_simulation(self):
        status, details, sim_calls, send_calls = self._run((True, None))
        self.assertEqual(len(sim_calls), 1)
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(status, "executed")

    def test_non_open_actions_skip_preflight(self):
        signal = make_signal()
        signal["action"] = "close"
        signal["position_id"] = "POSITION1111111111111111111111111111111111"
        with mock.patch.object(dispatcher, "live_preflight") as lp_mod, \
             mock.patch.object(dispatcher, "_get_dex_module",
                               return_value=(lambda s, c, r: s,
                                             type("B", (), {"build": staticmethod(
                                                 lambda s, c, r: {"dex": "meteora",
                                                                  "action": "close",
                                                                  "mode": "send"})}))), \
             mock.patch.object(dispatcher, "chain_simulate",
                               return_value={"ok": True, "simulation": {"ok": True}}), \
             mock.patch.object(dispatcher, "chain_send",
                               return_value={"signatures": []}):
            dispatcher.process_signal(dict(signal), dict(CFG), dict(RAILS))
        lp_mod.preflight_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
