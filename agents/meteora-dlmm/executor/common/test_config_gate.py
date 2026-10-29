#!/usr/bin/env python3
"""Fail-closed tests for the dispatcher start gate.

Regression target: on 2026-10-18 the pipeline gated on the *workspace*
agent.config.json (dry_run) while the executor loaded the *live* copy
(auto). The pipeline log said "(dry_run)" and the executor was
live-capable. These tests pin the invariant: one effective config, and
any disagreement refuses.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config_gate  # noqa: E402
from common import paths  # noqa: E402


def write_config(path: Path, mode: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": {"mode": mode}}), encoding="utf-8")
    return path


class GateTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.live = self.dir / "live" / "agent.config.json"
        self.ws = self.dir / "workspace" / "agent.config.json"
        self._env = {
            k: os.environ.get(k)
            for k in (config_gate.ENV_LIVE, config_gate.ENV_WORKSPACE)
        }

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def gate(self, signer=False, require_dry_run=False):
        return config_gate.check_gate(
            signer,
            live=self.live,
            workspace=self.ws,
            require_dry_run=require_dry_run,
        )


class SingleConfigTests(GateTestBase):
    def test_dry_run_without_workspace_copy_allowed(self):
        write_config(self.live, "dry_run")
        allowed, reason = self.gate()
        self.assertTrue(allowed, reason)
        self.assertIn("dry_run", reason)

    def test_missing_live_config_refuses(self):
        write_config(self.ws, "dry_run")
        allowed, reason = self.gate()
        self.assertFalse(allowed)
        self.assertIn("config missing", reason)

    def test_unknown_mode_refuses(self):
        write_config(self.live, "yolo")
        allowed, reason = self.gate()
        self.assertFalse(allowed)
        self.assertIn("not one of", reason)

    def test_malformed_json_refuses(self):
        self.live.parent.mkdir(parents=True, exist_ok=True)
        self.live.write_text("{not json", encoding="utf-8")
        allowed, reason = self.gate()
        self.assertFalse(allowed)
        self.assertIn("unreadable", reason)

    def test_missing_agent_mode_refuses(self):
        self.live.parent.mkdir(parents=True, exist_ok=True)
        self.live.write_text(json.dumps({"agent": {}}), encoding="utf-8")
        allowed, reason = self.gate()
        self.assertFalse(allowed)
        self.assertIn("agent.mode", reason)


class ConfirmEachModeTests(GateTestBase):
    """confirm_each is a real mode; the gate must not call it unknown.

    It was missing from KNOWN_MODES while being documented in
    agent.config.json and implemented by dispatcher._handle_confirm_each, so a
    legitimate confirm_each config was refused with "not one of [...]".
    """

    def test_confirm_each_is_a_known_mode(self):
        self.assertIn("confirm_each", config_gate.KNOWN_MODES)

    def test_confirm_each_with_signer_is_allowed(self):
        write_config(self.live, "confirm_each")
        write_config(self.ws, "confirm_each")
        allowed, reason = self.gate(signer=True)
        self.assertTrue(allowed, reason)
        self.assertIn("confirm_each", reason)

    def test_confirm_each_without_signer_refuses(self):
        """Approval ends in a signature, so no signer means every approval fails."""
        write_config(self.live, "confirm_each")
        allowed, reason = self.gate(signer=False)
        self.assertFalse(allowed)
        self.assertIn("no signer", reason)

    def test_confirm_each_is_refused_by_the_pipeline_runner(self):
        write_config(self.live, "confirm_each")
        write_config(self.ws, "confirm_each")
        allowed, reason = self.gate(signer=True, require_dry_run=True)
        self.assertFalse(allowed)
        self.assertIn("requires dry_run", reason)

    def test_every_documented_mode_is_known(self):
        """agent.config.json documents the modes; the gate must accept them."""
        documented = set(document_config_modes())
        self.assertTrue(documented)
        for mode in documented:
            with self.subTest(mode=mode):
                self.assertIn(mode, config_gate.KNOWN_MODES)


def document_config_modes() -> list:
    """Mode names documented in the shipped agent.config.json."""
    path = paths.agent_root() / "config" / "agent.config.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(((data.get("agent") or {}).get("mode_options") or {}).keys())


class DriftTests(GateTestBase):
    """The exact 2026-10-18 failure: gate file and executor file disagree."""

    def test_dry_run_vs_auto_refuses(self):
        write_config(self.live, "auto")
        write_config(self.ws, "dry_run")
        allowed, reason = self.gate(signer=True)
        self.assertFalse(allowed)
        self.assertIn("disagrees", reason)

    def test_auto_vs_dry_run_refuses(self):
        write_config(self.live, "dry_run")
        write_config(self.ws, "auto")
        allowed, reason = self.gate()
        self.assertFalse(allowed)
        self.assertIn("disagrees", reason)

    def test_agreeing_copies_allowed(self):
        write_config(self.live, "dry_run")
        write_config(self.ws, "dry_run")
        allowed, reason = self.gate()
        self.assertTrue(allowed, reason)
        self.assertIn("modes agree", reason)

    def test_unreadable_workspace_copy_refuses(self):
        write_config(self.live, "dry_run")
        self.ws.parent.mkdir(parents=True, exist_ok=True)
        self.ws.write_text("", encoding="utf-8")
        allowed, reason = self.gate()
        self.assertFalse(allowed)
        self.assertIn("workspace copy unusable", reason)


class SignerFailClosedTests(GateTestBase):
    def test_auto_without_signer_refuses(self):
        write_config(self.live, "auto")
        allowed, reason = self.gate(signer=False)
        self.assertFalse(allowed)
        self.assertIn("no signer", reason)

    def test_auto_with_signer_allowed(self):
        write_config(self.live, "auto")
        allowed, reason = self.gate(signer=True)
        self.assertTrue(allowed, reason)

    def test_dry_run_never_needs_signer(self):
        write_config(self.live, "dry_run")
        allowed, reason = self.gate(signer=False)
        self.assertTrue(allowed, reason)


class RequireDryRunTests(GateTestBase):
    """The scheduled pipeline must be dry_run-only, signer or not.

    Without this, a config edit to ``auto`` plus a signer in the unit
    environment would let an unattended timer trade with no owner in the
    loop. The pipeline passes ``--require-dry-run`` for exactly that reason.
    """

    def test_auto_with_auto_and_signer_present_is_refused(self):
        """The regression: both copies say auto, signer present, still refuse."""
        write_config(self.live, "auto")
        write_config(self.ws, "auto")
        allowed, reason = self.gate(signer=True, require_dry_run=True)
        self.assertFalse(allowed)
        self.assertIn("REFUSING", reason)
        self.assertIn("requires dry_run", reason)

    def test_auto_without_signer_is_also_refused(self):
        write_config(self.live, "auto")
        write_config(self.ws, "auto")
        allowed, reason = self.gate(signer=False, require_dry_run=True)
        self.assertFalse(allowed)
        self.assertIn("requires dry_run", reason)

    def test_dry_run_is_allowed_with_or_without_signer(self):
        write_config(self.live, "dry_run")
        write_config(self.ws, "dry_run")
        for signer in (False, True):
            allowed, reason = self.gate(signer=signer, require_dry_run=True)
            self.assertTrue(allowed, reason)
            self.assertIn("dry_run", reason)

    def test_require_dry_run_does_not_mask_drift(self):
        """Drift is still reported as drift, not as a dry_run violation."""
        write_config(self.live, "auto")
        write_config(self.ws, "dry_run")
        allowed, reason = self.gate(signer=True, require_dry_run=True)
        self.assertFalse(allowed)
        self.assertIn("disagrees", reason)

    def test_cli_flag_refuses_auto_with_signer(self):
        write_config(self.live, "auto")
        write_config(self.ws, "auto")
        rc = config_gate.main([
            "--live", str(self.live), "--workspace", str(self.ws),
            "--signer-present", "1", "--require-dry-run", "--json",
        ])
        self.assertEqual(rc, 1)

    def test_cli_without_flag_still_allows_auto_with_signer(self):
        """The flag is opt-in: the dispatcher's own gate is unchanged."""
        write_config(self.live, "auto")
        write_config(self.ws, "auto")
        rc = config_gate.main([
            "--live", str(self.live), "--workspace", str(self.ws),
            "--signer-present", "1", "--json",
        ])
        self.assertEqual(rc, 0)


class PathResolutionTests(GateTestBase):
    def test_live_path_is_the_executor_default(self):
        os.environ.pop(config_gate.ENV_LIVE, None)
        os.environ.pop(paths.ENV_AGENT_ROOT, None)
        resolved = config_gate.live_config_path()
        self.assertEqual(resolved, config_gate.LIVE_CONFIG_PATH)
        # The path is owned by common/paths.py: it must be a config file inside
        # the resolved agent tree, whichever tree that is (live /data or this
        # checkout), and never a module-local literal.
        self.assertEqual(resolved, paths.config_path())
        self.assertEqual(resolved.name, "agent.config.json")
        self.assertEqual(resolved.parent.parent, paths.agent_root())

    def test_env_override_wins(self):
        os.environ[config_gate.ENV_LIVE] = str(self.live)
        self.assertEqual(config_gate.live_config_path(), self.live)

    def test_config_loader_uses_the_same_path(self):
        """The loader must not carry its own hardcoded copy."""
        from common import config_loader

        self.assertEqual(config_loader.CONFIG_PATH, config_gate.live_config_path())


class CliTests(GateTestBase):
    def test_cli_exit_codes(self):
        write_config(self.live, "dry_run")
        self.assertEqual(
            config_gate.main(
                ["--live", str(self.live), "--workspace", str(self.ws), "--json"]
            ),
            0,
        )
        write_config(self.ws, "auto")
        self.assertEqual(
            config_gate.main(
                ["--live", str(self.live), "--workspace", str(self.ws), "--json"]
            ),
            1,
        )

    def test_cli_json_never_leaks_config_body(self):
        write_config(self.live, "dry_run")
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            config_gate.main(
                ["--live", str(self.live), "--workspace", str(self.ws), "--json"]
            )
        payload = json.loads(buf.getvalue())
        self.assertEqual(set(payload), {"allowed", "mode", "reason"})
        self.assertEqual(payload["mode"], "dry_run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
