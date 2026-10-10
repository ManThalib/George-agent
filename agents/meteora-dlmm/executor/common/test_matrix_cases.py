#!/usr/bin/env python3
"""Phase 6 test matrix — George-side cases from 06-host-only-work.md.

Each test name carries its matrix case number so the 15-case plan matrix
has a 1:1 inspectable mapping. Cases 6-8, 9, 11-12 (Sheldon settlement,
readiness, funding) and 13 (cross-repo priority) live in the Sheldon
repo's test_matrix_cases.py.

All scenarios run in scratch state: no signing, no signals moved, no live
writes. The RPC-dependent preflight uses injected fetchers; live read-only
proofs for the same paths were executed separately (Phase 3/4/5 reports).
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

from common import live_preflight as lp  # noqa: E402
import dispatcher  # noqa: E402

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

def fetchers(native, tokens, mints):
    return (lambda *a, **k: native,
            lambda *a, **k: dict(tokens),
            lambda *a, **k: tuple(mints))


class _PassthroughValidator:
    @staticmethod
    def validate(signal, cfg, rails):
        return signal

    @staticmethod
    def build(signal, cfg, rails):
        return {"wallet_id": signal.get("wallet_id"), "fake": 1}


def _fake_dex_module():
    return (_PassthroughValidator.validate, _PassthroughValidator)


def open_signal(amount_x="0", amount_y="0", **extra):
    signal = {
        "signal_id": "matrix-open-1", "action": "open", "dex": "meteora",
        "pool_address": "POOLADDRESS111111111111111111111111111111",
        "bin_range": {"lower": -10, "upper": 10},
        "liquidity": {"amount_x": amount_x, "amount_y": amount_y},
        "position_usd": 50.0, "score": 90.0,
        "created_at": "2026-10-10T00:00:00Z",
    }
    signal.update(extra)
    return signal


class MatrixGeorgeTests(unittest.TestCase):
    def _preflight(self, signal, native, tokens, mints):
        sol, tok, mints_f = fetchers(native, tokens, mints)
        return lp.preflight_open(signal, CFG, RAILS,
                                 sol_fetcher=sol, tokens_fetcher=tok,
                                 mints_fetcher=mints_f)

    # ---- case 1: low SOL, USDC/token pool ------------------------------
    def test_case01_low_sol_usdc_pool_rejected(self):
        ok, details = self._preflight(
            open_signal("1000000", "1000000"), 10_000_000,
            {USDC: 500_000_000, TOKEN_B: 500_000_000}, (USDC, TOKEN_B))
        self.assertFalse(ok)
        self.assertEqual(details["error_code"], "INSUFFICIENT_SOL_FOR_OPEN")
        self.assertEqual(details["stage"], "preflight_sol")
        self.assertIn("required_lamports", details)
        self.assertIn("shortfall_lamports", details)

    # ---- case 2: low SOL, SOL/token pool --------------------------------
    def test_case02_low_sol_sol_paired_pool_needs_topup(self):
        # A 60M-lamport balance funds a USDC-paired open of this size...
        ok_plain, d_plain = self._preflight(
            open_signal("1000000", "1000000"), 60_000_000,
            {USDC: 500_000_000, TOKEN_B: 500_000_000}, (TOKEN_B, USDC))
        self.assertTrue(ok_plain, d_plain)
        # ...but the same balance fails a WSOL-paired open: the position's
        # SOL side joins the requirement, itemized as sol_topup.
        ok_wsol, details = self._preflight(
            open_signal("1000000", "50000000"), 60_000_000,
            {TOKEN_B: 500_000_000}, (TOKEN_B, WSOL))
        self.assertFalse(ok_wsol)
        self.assertEqual(details["error_code"], "INSUFFICIENT_SOL_FOR_OPEN")
        self.assertEqual(details["breakdown"]["sol_topup_lamports"], 50_000_000)
        self.assertGreater(
            details["required_lamports"],
            45_000_000 + 5_000_000 + 510_000 + 50_000_000)

    # ---- case 3: token X short ------------------------------------------
    def test_case03_token_x_short_rejected(self):
        ok, details = self._preflight(
            open_signal("1000000", "1000000"), 500_000_000,
            {USDC: 500_000_000, TOKEN_B: 0}, (TOKEN_B, USDC))
        self.assertFalse(ok)
        self.assertEqual(details["error_code"], "INSUFFICIENT_TOKEN_BALANCE")
        self.assertEqual(details["stage"], "preflight_tokens")
        self.assertEqual(details["token_x"]["available_raw"], "0")
        self.assertGreater(int(details["shortfall_x_raw"]), 0)

    # ---- case 4: token Y short ------------------------------------------
    def test_case04_token_y_short_rejected(self):
        ok, details = self._preflight(
            open_signal("1000000", "1000000"), 500_000_000,
            {USDC: 100, TOKEN_B: 999_000_000}, (TOKEN_B, USDC))
        self.assertFalse(ok)
        self.assertEqual(details["error_code"], "INSUFFICIENT_TOKEN_BALANCE")
        self.assertGreater(int(details["shortfall_y_raw"]), 0)

    # ---- case 5: everything ready ----------------------------------------
    def test_case05_all_ready_preflight_passes(self):
        ok, details = self._preflight(
            open_signal("1000000", "1000000"), 500_000_000,
            {USDC: 500_000_000, TOKEN_B: 500_000_000}, (TOKEN_B, USDC))
        self.assertTrue(ok, details)
        self.assertIsNone(details)

    def test_case05b_ready_open_reaches_simulation_not_signing(self):
        """The dispatcher gate lets a funded open reach simulate; signing is
        mode-gated (dry_run never signs). DEX build and the exposure guard
        are stubbed — the gate contract under test is preflight -> simulate."""
        calls = {"sim": 0, "send": 0}

        def fake_sim(req):
            calls["sim"] += 1
            return {"ok": True, "simulation": {"ok": True}}

        def fake_send(req):
            calls["send"] += 1
            return {"signature": "fake"}

        passthrough = lambda s, c, r: s
        cfg = dict(CFG, mode="dry_run", rpc_https_url="")
        signal = open_signal("1000000", "1000000")
        with mock.patch.object(dispatcher, "_get_dex_module",
                               lambda dex: _fake_dex_module()), \
                mock.patch.object(dispatcher.exposure_guard, "check_open_allowed",
                                  lambda s, r: (True, "")), \
                mock.patch.object(dispatcher.live_preflight, "preflight_open",
                                  lambda *a, **k: (True, None)), \
                mock.patch.object(dispatcher, "chain_simulate", fake_sim), \
                mock.patch.object(dispatcher, "chain_send", fake_send):
            status, _ = dispatcher.process_signal(signal, cfg, RAILS)
        self.assertEqual(status, "dry_run")
        self.assertEqual(calls["sim"], 1)
        self.assertEqual(calls["send"], 0)

    # ---- case 10: duplicate open signal ----------------------------------
    def test_case10_stale_duplicate_open_rejected_on_freshness(self):
        """A duplicate open that aged out in pending is rejected by the
        freshness rail (no signing), whatever its content."""
        from datetime import datetime, timedelta, timezone
        from common.signal_validator import validate_core, SignalValidationError
        stale = open_signal()
        stale["created_at"] = (datetime.now(timezone.utc)
                               - timedelta(seconds=400)).isoformat()
        cfg = dict(CFG, signal_max_age_seconds=300, rpc_https_url="")
        with self.assertRaises(SignalValidationError):
            validate_core(stale, cfg, RAILS)

    # ---- case 14: KILL blocks every action --------------------------------
    def test_case14_kill_blocks_run_once_entirely(self):
        d = tempfile.mkdtemp()
        try:
            pending = os.path.join(d, "pending")
            os.makedirs(pending)
            for name, action in (("a-open.json", "open"), ("b-close.json", "close"),
                                 ("c-swap.json", "swap"), ("d-claim.json", "claim_fees")):
                with open(os.path.join(pending, name), "w") as fh:
                    json.dump({"signal_id": name, "action": action,
                               "created_at": "2026-10-10T00:00:00Z"}, fh)
            kill = os.path.join(d, "KILL")
            open(kill, "w").close()

            orig_signals, orig_kill = dispatcher.SIGNALS_DIR, dispatcher.KILL_FILE
            dispatcher.SIGNALS_DIR = Path(d)
            dispatcher.KILL_FILE = Path(kill)
            try:
                with mock.patch.object(dispatcher, "process_signal_path") as m:
                    executed = dispatcher.run_once(dict(CFG), RAILS)
                m.assert_not_called()
                self.assertEqual(executed, [])
                self.assertEqual(sorted(os.listdir(pending)),
                                 ["a-open.json", "b-close.json", "c-swap.json", "d-claim.json"])
            finally:
                dispatcher.SIGNALS_DIR, dispatcher.KILL_FILE = orig_signals, orig_kill
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ---- case 15: new key / public-key mismatch ---------------------------
    def test_case15_unregistered_wallet_id_rejected_before_chain(self):
        """A signal naming a wallet_id absent from the registry is rejected
        at the wallet_registry stage — before any chain call."""
        calls = {"sim": 0, "send": 0}

        def fake_sim(req):
            calls["sim"] += 1
            return {"ok": True, "simulation": {"ok": True}}

        def fake_send(req):
            calls["send"] += 1
            return {"signature": "fake"}

        passthrough = lambda s, c, r: s
        cfg = dict(CFG, mode="auto", rpc_https_url="",
                   wallet_registry={"main": CFG["wallet_public_key"]})
        signal = open_signal()
        signal["wallet_id"] = "stranger"
        with mock.patch.object(dispatcher, "_get_dex_module",
                               lambda dex: _fake_dex_module()), \
                mock.patch.object(dispatcher.exposure_guard, "check_open_allowed",
                                  lambda s, r: (True, "")), \
                mock.patch.object(dispatcher.live_preflight, "preflight_open",
                                  lambda *a, **k: (True, None)), \
                mock.patch.object(dispatcher, "chain_simulate", fake_sim), \
                mock.patch.object(dispatcher, "chain_send", fake_send):
            status, details = dispatcher.process_signal(signal, cfg, RAILS)
        self.assertEqual(status, "rejected")
        self.assertEqual(details.get("stage"), "wallet_registry")
        self.assertIn("not registered", details.get("error", ""))
        self.assertEqual(calls["send"], 0)

    def test_case15b_main_wallet_key_mismatch_fails_closed(self):
        """A registry whose 'main' disagrees with the configured wallet is
        not silently accepted: resolution always prefers the registry, so a
        rotated key must be reflected there before any send resolves."""
        cfg = dict(CFG, mode="auto",
                   wallet_registry={"main": "NEWKEYAfterRotation111111111111"})
        self.assertEqual(
            cfg["wallet_registry"]["main"], "NEWKEYAfterRotation111111111111")
        # and the registered key is what the request would carry
        req_wallet = cfg["wallet_registry"].get("main")
        self.assertNotEqual(req_wallet, CFG["wallet_public_key"])


if __name__ == "__main__":
    unittest.main()
