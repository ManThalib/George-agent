"""Tests for the open-signal live-tick containment guard.

The ZEC/USDC incident: open signals centered at tick 0 (stale pool scans)
executed far out of range. The validator must reject an open whose
bin_range misses the pool's live current tick.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.signal_validator import SignalValidationError, validate_core  # noqa: E402


CFG = {"signal_max_age_seconds": 300, "rpc_https_url": "https://rpc.test"}
RAILS = {
    "max_slippage_bps": 100,
    "min_position_usd": 10.0,
    "max_position_usd": 100.0,
}


def _open_signal(lower, upper, dex="orca"):
    return {
        "signal_id": "tick-guard-test",
        "action": "open",
        "dex": dex,
        "pool_address": "GTHKH8s82ZR8GTSFZ1dUu6wfdxhy59wpMShxzG5zjiPm",
        "side": "bidirectional",
        "bin_range": {"lower": lower, "upper": upper},
        "liquidity": {"amount_x": "1000", "amount_y": "1000"},
        "position_usd": 50.0,
        "max_slippage_bps": 100,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


class TickContainmentGuardTests(unittest.TestCase):
    @patch("common.signal_validator.fetch_pool_current_tick", return_value=26191)
    def test_range_containing_tick_passes(self, _mock):
        signal = validate_core(_open_signal(26000, 26400), CFG, RAILS)
        self.assertEqual(signal["action"], "open")

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=26191)
    def test_range_boundary_tick_passes(self, _mock):
        # Tick exactly on an edge is contained.
        signal = validate_core(_open_signal(26191, 26500), CFG, RAILS)
        self.assertEqual(signal["action"], "open")

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=26191)
    def test_stale_range_missing_tick_rejected(self, _mock):
        # The incident shape: range centered at 0 while live tick ~26191.
        with self.assertRaises(SignalValidationError) as ctx:
            validate_core(_open_signal(-70, 70), CFG, RAILS)
        self.assertIn("does not contain", str(ctx.exception))

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=26191)
    def test_meteora_bin_guard_applies_too(self, _mock):
        with self.assertRaises(SignalValidationError):
            validate_core(_open_signal(-5400, -5200, dex="meteora"), CFG, RAILS)

    @patch("common.signal_validator.fetch_pool_current_tick", return_value=None)
    def test_fetch_failure_warns_but_does_not_block(self, _mock):
        signal = validate_core(_open_signal(-70, 70), CFG, RAILS)
        self.assertEqual(signal["action"], "open")

    def test_no_rpc_url_skips_guard_without_fetch(self):
        cfg = dict(CFG, rpc_https_url="")
        with patch("common.signal_validator.fetch_pool_current_tick") as mock:
            # Empty rpc_url makes the real fetch return None without dialing.
            mock.return_value = None
            signal = validate_core(_open_signal(-70, 70), cfg, RAILS)
        mock.assert_called_once()
        self.assertEqual(signal["action"], "open")


class TickFetchDecodeTests(unittest.TestCase):
    """fetch_pool_current_tick decodes i32 at the per-DEX offsets."""

    def _rpc_response(self, offset, value, size=653):
        import base64
        import json as json_mod
        from unittest.mock import MagicMock

        blob = bytearray(size)
        blob[offset : offset + 4] = value.to_bytes(4, "little", signed=True)
        body = json_mod.dumps(
            {"result": {"value": {"data": [base64.b64encode(bytes(blob)).decode(), "base64"]}}}
        ).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("common.signal_validator.urllib.request.urlopen")
    def test_orca_tick_decoded_at_offset_81(self, mock_urlopen):
        from common.signal_validator import fetch_pool_current_tick

        mock_urlopen.return_value = self._rpc_response(81, 26726)
        tick = fetch_pool_current_tick("orca", "PoolAddr", "https://rpc.test")
        self.assertEqual(tick, 26726)

    @patch("common.signal_validator.urllib.request.urlopen")
    def test_meteora_bin_decoded_at_offset_76(self, mock_urlopen):
        from common.signal_validator import fetch_pool_current_tick

        mock_urlopen.return_value = self._rpc_response(76, -5333)
        tick = fetch_pool_current_tick("meteora", "PoolAddr", "https://rpc.test")
        self.assertEqual(tick, -5333)

    def test_unknown_dex_returns_none_without_rpc_call(self):
        from common.signal_validator import fetch_pool_current_tick

        with patch("common.signal_validator.urllib.request.urlopen") as mock:
            self.assertIsNone(fetch_pool_current_tick("uniswap", "Addr", "https://rpc.test"))
        mock.assert_not_called()

    def test_empty_rpc_url_returns_none(self):
        from common.signal_validator import fetch_pool_current_tick

        self.assertIsNone(fetch_pool_current_tick("orca", "Addr", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
