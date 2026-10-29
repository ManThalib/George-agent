#!/usr/bin/env python3
"""RPC credentials must not escape through chain-dispatch error text.

`chain/dispatch.js` echoes offending fallback URLs verbatim:

    fallback_rpc_urls must be https URLs, got: https://host/?api-key=***

`chain_client` used to re-raise that string unchanged, so the key landed in
`/tmp/george.log` and in any exception trace. Redaction now happens at the
Python boundary, which covers every current and future JS throw without
touching `dispatch.js`.

Scope is deliberately narrow: only the three message-construction sites are
sanitized (returncode error, invalid-JSON stdout snippet, response error
field). The request payload and functional fields are untouched, and no
signing/sending logic is involved.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common.chain_client as chain_client  # noqa: E402
from common.chain_client import ChainError, _run  # noqa: E402

# Non-secret placeholder shaped like a provider key.
KEY = "0123456789abcdef0123456789abcdef"
LEAKY_URL = "https://mainnet.helius-rpc.com/?api-key=" + KEY

REQ = {"dex": "meteora", "action": "open", "mode": "simulate",
       "rpc_url": "https://api.mainnet-beta.solana.com",
       "wallet_public_key": "B8qv53pKyW2sgP4VYik1GkkZ6NHNZZn6K5VTyVm55tTa"}


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["node"], returncode=returncode, stdout=stdout, stderr=stderr)


class _Base(unittest.TestCase):
    def setUp(self):
        # DISPATCH_JS must appear to exist; no node process is ever spawned.
        p = patch.object(chain_client, "DISPATCH_JS", Path("/dev/null"))
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, **proc_kwargs):
        with patch.object(chain_client.subprocess, "run",
                          return_value=_proc(**proc_kwargs)):
            with self.assertRaises(ChainError) as ctx:
                _run(dict(REQ))
        return str(ctx.exception)


class ReturncodeErrorTests(_Base):
    def test_fallback_url_with_api_key_never_appears(self):
        """The exact dispatch.js:47 leak: full URL joined into an error."""
        msg = self.run_with(
            returncode=1,
            stdout=json.dumps({
                "ok": False, "stage": "dispatch",
                "error": f"fallback_rpc_urls must be https URLs, got: {LEAKY_URL}",
            }))
        self.assertNotIn(KEY, msg)
        for n in (4, 8, 12, 16, 24, 32):
            self.assertNotIn(KEY[:n], msg, f"{n}-char key prefix leaked")
        self.assertNotIn("api-key", msg)
        # Diagnostics survive.
        self.assertIn("chain dispatcher failed (1)", msg)
        self.assertIn("fallback_rpc_urls must be https URLs", msg)
        self.assertIn("mainnet.helius-rpc.com", msg)

    def test_plain_text_stdout_is_also_sanitized(self):
        msg = self.run_with(returncode=2, stdout=f"boom {LEAKY_URL} trailing")
        self.assertNotIn(KEY, msg)
        self.assertIn("boom", msg)
        self.assertIn("trailing", msg)
        self.assertIn("chain dispatcher failed (2)", msg)

    def test_non_json_dict_stdout_is_sanitized(self):
        """err is a parsed dict here; its repr must be redacted too."""
        msg = self.run_with(returncode=1,
                            stdout=json.dumps({"error": LEAKY_URL}))
        self.assertNotIn(KEY, msg)


class InvalidJsonTests(_Base):
    def test_stdout_snippet_is_sanitized(self):
        msg = self.run_with(returncode=0,
                            stdout=f'{{"ok": true, "note": "{LEAKY_URL}"')
        self.assertNotIn(KEY, msg)
        for n in (4, 8, 16, 32):
            self.assertNotIn(KEY[:n], msg, f"{n}-char key prefix leaked")
        self.assertIn("invalid JSON", msg)
        self.assertIn("stdout:", msg)

    def test_snippet_is_still_bounded_to_500_chars(self):
        msg = self.run_with(returncode=0, stdout="x" * 2000)
        self.assertIn("stdout: " + "x" * 500, msg)
        self.assertNotIn("x" * 501, msg)


class ResponseErrorFieldTests(_Base):
    def test_error_field_is_sanitized(self):
        msg = self.run_with(
            returncode=0,
            stdout=json.dumps({"ok": False, "stage": "send",
                               "error": f"RPC rejected {LEAKY_URL}"}))
        self.assertNotIn(KEY, msg)
        self.assertIn("at stage send", msg)
        self.assertIn("RPC rejected", msg)

    def test_stage_is_preserved(self):
        msg = self.run_with(returncode=0,
                            stdout=json.dumps({"ok": False, "stage": "build"}))
        self.assertIn("at stage build", msg)
        self.assertIn("unknown error", msg)


class SemanticsUnchangedTests(_Base):
    """Redaction must not alter control flow, types, or messages without URLs."""

    def test_message_without_url_is_byte_identical(self):
        msg = self.run_with(returncode=3, stdout="plain failure text")
        self.assertEqual(msg, "chain dispatcher failed (3): plain failure text")

    def test_success_path_is_untouched(self):
        payload = {"ok": True, "signature": "abc", "rpc_url": LEAKY_URL}
        with patch.object(chain_client.subprocess, "run",
                          return_value=_proc(stdout=json.dumps(payload))):
            out = _run(dict(REQ))
        # Functional fields are NOT redacted; only error text is.
        self.assertEqual(out, payload)

    def test_request_payload_is_not_mutated(self):
        req = dict(REQ)
        with patch.object(chain_client.subprocess, "run",
                          return_value=_proc(stdout=json.dumps({"ok": True}))):
            _run(req)
        self.assertEqual(req, REQ)

    def test_chain_error_type_is_unchanged(self):
        with patch.object(chain_client.subprocess, "run",
                          return_value=_proc(returncode=1, stdout="x")):
            with self.assertRaises(ChainError):
                _run(dict(REQ))

    def test_missing_dispatcher_still_raises_chain_error(self):
        with patch.object(chain_client, "DISPATCH_JS",
                          Path("/nonexistent/dispatch.js")):
            with self.assertRaises(ChainError) as ctx:
                _run(dict(REQ))
        self.assertIn("chain dispatcher missing", str(ctx.exception))

    def test_dispatch_js_error_text_is_unchanged_at_source(self):
        """We redact at the Python boundary; the JS throw stays as-is."""
        src = (Path(__file__).resolve().parent.parent / "chain" /
               "dispatch.js").read_text(encoding="utf-8")
        self.assertIn("badFallbacks.join(\", \")", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
