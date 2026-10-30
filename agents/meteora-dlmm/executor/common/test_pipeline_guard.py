#!/usr/bin/env python3
"""Regression: the scheduled pipeline must never start a live-capable dispatcher.

`run_pipeline.sh` used to *claim* it never starts the dispatcher outside
dry_run, while the gate only demanded a signer for `auto`. Agreeing
auto+auto plus a signer in the unit environment would therefore have started
a live-capable dispatcher from an unattended timer — a policy in a comment
that the code did not enforce.

These tests pin the enforcement, not the comment:

  1. the gate CLI refuses auto+auto even with signer-present=1
  2. it refuses for any non-dry_run mode, not just "auto"
  3. it still allows dry_run (with or without a signer)
  4. run_pipeline.sh passes --require-dry-run
  5. the gate runs BEFORE the dispatcher is launched in that script
  6. a refused gate exits non-zero, so the dispatcher line is unreachable

Hermetic: temp config files, no network, no live data dir, no real signer.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXECUTOR = Path(__file__).resolve().parent.parent
GATE = EXECUTOR / "common" / "config_gate.py"
PIPELINE = Path("/root/.openclaw-devdash/workspace/run_pipeline.sh")

# A non-secret placeholder: proves "signer present" without a real key.
SIGNER = "placeholder-not-a-key"


def _write_cfg(path: Path, mode: str, public_key: str = None) -> None:
    cfg = {"agent": {"mode": mode}}
    if public_key:
        cfg["wallet"] = {"public_key": public_key}
    path.write_text(json.dumps(cfg), encoding="utf-8")


# Two distinct public-shaped addresses for drift tests (not keys).
OLD_WALLET = "Test01dWa11etCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
NEW_WALLET = "TestNewWa11etDDDDDDDDDDDDDDDDDDDDDDDDDDDD"


class GateCliTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.live = self.dir / "live" / "agent.config.json"
        self.ws = self.dir / "workspace" / "agent.config.json"
        self.live.parent.mkdir(parents=True)
        self.ws.parent.mkdir(parents=True)

    def run_gate(self, live_mode: str, ws_mode: str, signer: bool = True,
                 require_dry_run: bool = True):
        _write_cfg(self.live, live_mode)
        _write_cfg(self.ws, ws_mode)
        return self.run_gate_raw(signer=signer,
                                 require_dry_run=require_dry_run)

    def run_gate_raw(self, signer: bool = False, require_dry_run: bool = True):
        """Run against configs already written by the caller."""
        cmd = [sys.executable, str(GATE), "--json"]
        if require_dry_run:
            cmd.append("--require-dry-run")
        cmd += ["--signer-present", "1" if signer else "0"]
        env = dict(os.environ)
        env["GEORGE_CONFIG_PATH"] = str(self.live)
        env["GEORGE_WORKSPACE_CONFIG_PATH"] = str(self.ws)
        if signer:
            env["SOLANA_AGENT_WALLET"] = SIGNER
        else:
            env.pop("SOLANA_AGENT_WALLET", None)
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=60)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        return proc.returncode, payload

class RefusesLiveRunnerTests(GateCliTestBase):
    def test_auto_auto_with_signer_is_refused(self):
        """The exact scenario: both configs agree on auto, signer present."""
        rc, out = self.run_gate("auto", "auto", signer=True)
        self.assertEqual(rc, 1)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["mode"], "auto")
        self.assertIn("requires dry_run", out["reason"])
        self.assertIn("separate owner authorization", out["reason"])

    def test_refusal_reason_never_leaks_the_signer(self):
        rc, out = self.run_gate("auto", "auto", signer=True)
        self.assertNotIn(SIGNER, json.dumps(out))

    def test_auto_without_signer_is_also_refused(self):
        """dry_run-only must not depend on signer presence at all."""
        rc, out = self.run_gate("auto", "auto", signer=False)
        self.assertEqual(rc, 1)
        self.assertFalse(out["allowed"])

    def test_any_non_dry_run_mode_is_refused(self):
        for mode in ("live", "enabled", "AUTO", "Auto", "dry-run", "dryrun"):
            with self.subTest(mode=mode):
                rc, out = self.run_gate(mode, mode, signer=True)
                self.assertEqual(rc, 1, f"{mode!r} was not refused")
                self.assertFalse(out["allowed"])

    def test_dry_run_is_allowed_with_signer(self):
        rc, out = self.run_gate("dry_run", "dry_run", signer=True)
        self.assertEqual(rc, 0)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["mode"], "dry_run")

    def test_dry_run_is_allowed_without_signer(self):
        rc, out = self.run_gate("dry_run", "dry_run", signer=False)
        self.assertEqual(rc, 0)
        self.assertTrue(out["allowed"])

    def test_mode_drift_is_still_reported_as_drift(self):
        """require_dry_run must not mask a config split."""
        rc, out = self.run_gate("dry_run", "auto", signer=True)
        self.assertEqual(rc, 1)
        self.assertFalse(out["allowed"])
        self.assertIn("disagrees", out["reason"])

    def test_wallet_key_drift_is_refused(self):
        """2026-10-19 rotation: live copy still tracked the retired wallet.

        Signing with one key while every report tracks another is the same
        failure class as mode drift, so it refuses rather than guessing.
        """
        _write_cfg(self.live, "dry_run", public_key=OLD_WALLET)
        _write_cfg(self.ws, "dry_run", public_key=NEW_WALLET)
        rc, out = self.run_gate_raw()
        self.assertEqual(rc, 1)
        self.assertFalse(out["allowed"])
        self.assertIn("public_key disagrees", out["reason"])
        # A drift report must not publish either address in full.
        self.assertNotIn(OLD_WALLET, out["reason"])
        self.assertNotIn(NEW_WALLET, out["reason"])

    def test_wallet_key_agreement_is_allowed(self):
        _write_cfg(self.live, "dry_run", public_key=NEW_WALLET)
        _write_cfg(self.ws, "dry_run", public_key=NEW_WALLET)
        rc, out = self.run_gate_raw()
        self.assertEqual(rc, 0)
        self.assertTrue(out["allowed"])

    def test_gate_without_the_flag_still_allows_auto_with_signer(self):
        """The dispatcher's own gate keeps its original semantics."""
        rc, out = self.run_gate("auto", "auto", signer=True, require_dry_run=False)
        self.assertEqual(rc, 0)
        self.assertTrue(out["allowed"])


@unittest.skipUnless(PIPELINE.exists(), "run_pipeline.sh not present")
class PipelineWiringTests(unittest.TestCase):
    def setUp(self):
        self.src = PIPELINE.read_text(encoding="utf-8")

    def test_pipeline_passes_require_dry_run(self):
        self.assertIn("--require-dry-run", self.src)

    def test_gate_runs_before_the_dispatcher_is_launched(self):
        gate_at = self.src.index("config_gate.py")
        dispatch_at = self.src.index("executor/dispatcher.py --once")
        self.assertLess(gate_at, dispatch_at,
                        "dispatcher is launched before the gate is consulted")

    def test_refusal_exits_non_zero(self):
        """A refused gate must abort the script, not fall through."""
        after_gate = self.src[self.src.index("config_gate.py"):]
        self.assertIn("exit 1", after_gate.split("dispatcher.py --once")[0])

    def test_gate_failure_is_not_swallowed(self):
        # The gate now runs at step 0.5, so "everything up to the dispatcher"
        # would span the whole script and weaken this into a no-op. Scope it
        # to the gate block: from the gate invocation to the export of its
        # result.
        gate_line_block = self.src[self.src.index("config_gate.py"):
                                   self.src.index("export PIPELINE_AGENT_MODE")]
        self.assertNotIn("|| true", gate_line_block)
        self.assertIn("exit 1", gate_line_block)

    def test_signer_presence_is_computed_without_echoing_the_value(self):
        self.assertIn('"${SOLANA_AGENT_WALLET:+1}"', self.src)
        self.assertNotIn("echo $SOLANA_AGENT_WALLET", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
