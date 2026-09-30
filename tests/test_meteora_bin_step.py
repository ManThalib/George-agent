"""Tests for the Meteora bin-step rail: no opens on pools whose on-chain
bin_step is not in SAFETY_RAILS.md `allowed_bin_steps` (min 10).

Incident: a live position was opened on pool 5rCf1DM8LjKTw4YqhnoLcngyZYeNnQq
ztScTogYHAS6 (SOL-USDC, bin_step 4) — the rail was declared but never
enforced. The bin_step is read from the pool's live on-chain account
(LbPair.bin_step, u16 @ 80), never from signal metadata.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_EXECUTOR_DIR = Path(__file__).resolve().parent.parent / "agents" / "meteora-dlmm" / "executor"
sys.path.insert(0, str(_EXECUTOR_DIR))

from common import signal_validator  # noqa: E402
from common.signal_validator import SignalValidationError  # noqa: E402
from dexes.meteora import validator as meteora_validator  # noqa: E402


INCIDENT_POOL = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
CFG = {"signal_max_age_seconds": 300, "rpc_https_url": "https://rpc.test"}
RAILS = {
    "max_slippage_bps": 100,
    "max_meteora_range_width": 70,
    "allowed_bin_steps": [10, 20, 25, 50, 100],
}


def _open_signal(lower=-5322, upper=-5276, pool=INCIDENT_POOL):
    return {
        "signal_id": "bin-step-test",
        "action": "open",
        "dex": "meteora",
        "pool_address": pool,
        "side": "bidirectional",
        "bin_range": {"lower": lower, "upper": upper},
        "liquidity": {"amount_x": "1000", "amount_y": "1000"},
        "max_slippage_bps": 100,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _validate(signal, rails=None):
    return meteora_validator.validate(signal, CFG, rails if rails is not None else RAILS)


class MeteoraBinStepRailTests(unittest.TestCase):
    """The validator is the hard enforcement layer."""

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=4)
    def test_bin_step_4_rejected(self, _step, _tick):
        with self.assertRaises(SignalValidationError) as ctx:
            _validate(_open_signal())
        self.assertIn("not in allowed_bin_steps", str(ctx.exception))
        self.assertIn("4", str(ctx.exception))

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=10)
    def test_bin_step_10_passes(self, _step, _tick):
        signal = _validate(_open_signal())
        self.assertEqual(signal["action"], "open")

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=100)
    def test_bin_step_100_passes(self, _step, _tick):
        signal = _validate(_open_signal())
        self.assertEqual(signal["action"], "open")

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=None)
    def test_fetch_failure_fails_closed(self, _step, _tick):
        with self.assertRaises(SignalValidationError) as ctx:
            _validate(_open_signal())
        self.assertIn("cannot verify pool bin_step", str(ctx.exception))

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=4)
    def test_missing_rail_falls_back_to_declared_default(self, _step, _tick):
        rails = {"max_slippage_bps": 100, "max_meteora_range_width": 70}
        with self.assertRaises(SignalValidationError):
            _validate(_open_signal(), rails)

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=10)
    def test_missing_rail_fallback_still_allows_10(self, _step, _tick):
        rails = {"max_slippage_bps": 100, "max_meteora_range_width": 70}
        signal = _validate(_open_signal(), rails)
        self.assertEqual(signal["action"], "open")

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=10)
    def test_tighter_custom_rail_rejects_10(self, _step, _tick):
        rails = dict(RAILS, allowed_bin_steps=[25, 50])
        with self.assertRaises(SignalValidationError) as ctx:
            _validate(_open_signal(), rails)
        self.assertIn("not in allowed_bin_steps", str(ctx.exception))

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=25)
    def test_tighter_custom_rail_allows_25(self, _step, _tick):
        rails = dict(RAILS, allowed_bin_steps=[25, 50])
        signal = _validate(_open_signal(), rails)
        self.assertEqual(signal["action"], "open")

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=10)
    def test_malformed_rail_falls_back_to_default(self, _step, _tick):
        rails = dict(RAILS, allowed_bin_steps="nonsense")
        signal = _validate(_open_signal(), rails)
        self.assertEqual(signal["action"], "open")

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=-5299)
    @patch("common.signal_validator.fetch_pool_bin_step", return_value=10)
    def test_oversized_range_still_rejected_by_width_rail(self, _step, _tick):
        with self.assertRaises(SignalValidationError) as ctx:
            _validate(_open_signal(-5322, -5100))
        self.assertIn("exceeds rail", str(ctx.exception))

    @patch("common.signal_validator.fetch_pool_current_tick")
    @patch("common.signal_validator.fetch_pool_bin_step")
    def test_close_action_never_fetches(self, step_mock, tick_mock):
        signal = {
            "signal_id": "close-test",
            "action": "close",
            "dex": "meteora",
            "pool_address": INCIDENT_POOL,
            "position_id": "POS1",
            "max_slippage_bps": 100,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        out = _validate(signal)
        self.assertEqual(out["action"], "close")
        step_mock.assert_not_called()
        tick_mock.assert_not_called()


class BinStepFetchDecodeTests(unittest.TestCase):
    """fetch_pool_bin_step decodes u16 at the Meteora LbPair offset 80."""

    def _rpc_response(self, offset, value, size=653):
        import base64
        import json as json_mod

        blob = bytearray(size)
        blob[offset : offset + 2] = int(value).to_bytes(2, "little", signed=False)
        body = json_mod.dumps(
            {"result": {"value": {"data": [base64.b64encode(bytes(blob)).decode(), "base64"]}}}
        ).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("common.signal_validator.urllib.request.urlopen")
    def test_meteora_bin_step_decoded_at_offset_80(self, mock_urlopen):
        mock_urlopen.return_value = self._rpc_response(80, 4)
        step = signal_validator.fetch_pool_bin_step("meteora", "Addr", "https://rpc.test")
        self.assertEqual(step, 4)

    @patch("common.signal_validator.urllib.request.urlopen")
    def test_short_blob_returns_none(self, mock_urlopen):
        import base64
        import json as json_mod

        # 81-byte account: the u16 at offset 80 would need bytes 80..82.
        blob = bytes(81)
        body = json_mod.dumps(
            {"result": {"value": {"data": [base64.b64encode(blob).decode(), "base64"]}}}
        ).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        self.assertIsNone(
            signal_validator.fetch_pool_bin_step("meteora", "Addr", "https://rpc.test")
        )

    def test_unknown_dex_returns_none_without_rpc_call(self):
        with patch("common.signal_validator.urllib.request.urlopen") as mock:
            self.assertIsNone(
                signal_validator.fetch_pool_bin_step("orca", "Addr", "https://rpc.test")
            )
        mock.assert_not_called()

    def test_empty_rpc_url_returns_none(self):
        self.assertIsNone(signal_validator.fetch_pool_bin_step("meteora", "Addr", ""))


class RailsLoaderListParsingTests(unittest.TestCase):
    """SAFETY_RAILS.md is the source of truth; its allowed_bin_steps row
    must actually parse (backticked keys, bracketed int lists)."""

    def test_parse_markdown_rails_reads_allowed_bin_steps(self):
        from common.rails_loader import RAILS_PATH, _parse_markdown_rails

        rails = _parse_markdown_rails(RAILS_PATH)
        self.assertEqual(rails["allowed_bin_steps"], [10, 20, 25, 50, 100])

    def test_parse_int_list_formats(self):
        from common.rails_loader import _parse_int_list

        self.assertEqual(_parse_int_list("[10, 20, 25, 50, 100]"), [10, 20, 25, 50, 100])
        self.assertEqual(_parse_int_list("10,20"), [10, 20])

    def test_parse_int_list_rejects_junk(self):
        from common.rails_loader import _parse_int_list

        with self.assertRaises(ValueError):
            _parse_int_list("nonsense")


if __name__ == "__main__":
    unittest.main(verbosity=2)
