#!/usr/bin/env python3
"""RPC URLs must never be logged with their credential.

``dispatcher.py --check`` printed ``rpc_https_url[:60] + "..."``. For a
provider URL like

    https://mainnet.helius-rpc.com/?api-key=0123456789abcdef0123456789abcdef

that prefix exposes ``?api-key=0123456789abcdef...`` — a recoverable chunk of
the key, and for a short key possibly all of it. Truncation is not redaction.

Contract pinned here: only scheme/host/port/path survive; query, fragment and
userinfo are removed whole; unparseable input is never echoed.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.redact import MASK, redact_url, redact_urls_in  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"


class UrlRedactionTests(unittest.TestCase):
    def test_api_key_query_is_removed(self):
        out = redact_url(f"https://mainnet.helius-rpc.com/?api-key={KEY}")
        self.assertEqual(out, f"https://mainnet.helius-rpc.com/?{MASK}")
        self.assertNotIn(KEY, out)
        self.assertNotIn("api-key", out)

    def test_no_prefix_of_the_key_survives(self):
        out = redact_url(f"https://mainnet.helius-rpc.com/?api-key={KEY}")
        for n in (4, 8, 12, 16, 24, 32):
            self.assertNotIn(KEY[:n], out, f"{n}-char prefix leaked")

    def test_host_and_path_still_identify_the_provider(self):
        out = redact_url(f"https://rpc.example.com/solana/v1?key={KEY}")
        self.assertIn("rpc.example.com", out)
        self.assertIn("/solana/v1", out)

    def test_port_is_preserved(self):
        out = redact_url(f"http://127.0.0.1:8899/?k={KEY}")
        self.assertEqual(out, f"http://127.0.0.1:8899/?{MASK}")

    def test_userinfo_is_stripped(self):
        out = redact_url(f"https://alice:{KEY}@rpc.example.com/path")
        self.assertNotIn(KEY, out)
        self.assertNotIn("alice", out)
        self.assertIn("rpc.example.com", out)
        self.assertIn("credentials stripped", out)

    def test_fragment_is_stripped(self):
        out = redact_url(f"https://rpc.example.com/x#{KEY}")
        self.assertNotIn(KEY, out)
        self.assertIn(f"#{MASK}", out)

    def test_clean_url_is_unchanged(self):
        self.assertEqual(
            redact_url("https://api.mainnet-beta.solana.com"),
            "https://api.mainnet-beta.solana.com",
        )

    def test_empty_and_none(self):
        self.assertEqual(redact_url(""), "<none>")
        self.assertEqual(redact_url(None), "<none>")
        self.assertEqual(redact_url("   "), "<none>")

    def test_bare_secret_is_not_echoed(self):
        """A non-URL input might be the key itself; return a placeholder."""
        self.assertEqual(redact_url(KEY), "<not-a-url>")

    def test_unparseable_is_not_echoed(self):
        self.assertEqual(redact_url("https://"), "<not-a-url>")

    def test_non_string_is_not_echoed(self):
        self.assertEqual(redact_url(12345), "<none>")


class TextRedactionTests(unittest.TestCase):
    def test_urls_inside_exception_text_are_redacted(self):
        text = f"RPCError: POST https://mainnet.helius-rpc.com/?api-key={KEY} failed"
        out = redact_urls_in(text)
        self.assertNotIn(KEY, out)
        self.assertIn("RPCError", out)
        self.assertIn("mainnet.helius-rpc.com", out)

    def test_multiple_urls(self):
        text = f"a https://x.io/?k={KEY} b http://y.io/z?t={KEY} c"
        out = redact_urls_in(text)
        self.assertNotIn(KEY, out)
        self.assertIn("x.io", out)
        self.assertIn("y.io", out)

    def test_plain_text_passes_through(self):
        self.assertEqual(redact_urls_in("no urls here"), "no urls here")


class DispatcherCheckWiringTests(unittest.TestCase):
    """The health check must use the redactor, not slicing."""

    def test_do_check_does_not_slice_the_url(self):
        src = (
            Path(__file__).resolve().parent.parent / "dispatcher.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("rpc_https_url', '')[:60]", src)
        self.assertIn("redact_url(rpc)", src)
        # And the wallet is never printed in full either.
        self.assertIn("_redact_key(wallet)", src)

    def test_do_check_output_contains_no_api_key_substring(self):
        """End-to-end: run do_check and scan everything it printed."""
        import contextlib
        import io
        import os

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import dispatcher  # noqa: E402

        key = KEY
        url = "https://mainnet.helius-rpc.com/?api-key=" + key
        cfg = {
            "mode": "dry_run",
            "wallet_public_key": "Test01dWa11etCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            "rpc_https_url": url,
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # The environment takes precedence over the file (that is what the
            # executor actually uses), so pin both to the same value.
            with patch.dict(os.environ, {"SOLANA_PUBLIC_WALLET": "x",
                                         "SOLANA_RPC_URL": url}):
                dispatcher.do_check(cfg)
        out = buf.getvalue()

        # No part of the credential, at any useful length.
        for n in (4, 8, 12, 16, 20, 24, 32):
            self.assertNotIn(key[:n], out, f"{n}-char key prefix in --check output")
        self.assertNotIn("api-key", out)
        # The placeholder is expected; it carries no information.
        self.assertIn("?" + MASK, out)
        # The endpoint is still identifiable.
        self.assertIn("mainnet.helius-rpc.com", out)
        self.assertIn("mode: dry_run", out)

    def test_do_check_missing_url_does_not_crash(self):
        import contextlib
        import io

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import dispatcher  # noqa: E402

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dispatcher.do_check({"mode": "dry_run"})
        self.assertIn("rpc: <none>", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
