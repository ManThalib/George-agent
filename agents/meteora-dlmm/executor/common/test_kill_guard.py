"""KILL switch: the halt must be *identifiable*, not just quiet.

2026-10-19 (owner-approved): before this, an owner-initiated KILL exited 0 and
the driver printed "Pipeline run complete" — a killed cycle looked exactly like
a healthy one to systemd's OnFailure, to any webhook, and to a human reading
the last line. Exit 75 (EXIT_KILLED) plus a "KILLED" marker makes the state
explicit, and the alert script treats 75 as an expected stop rather than a
fault.

Two layers are covered here because they fail differently:

* dispatcher `--once` (authoritative: it is the only thing that can execute)
* `run_pipeline.sh` (wasted work + misleading "complete" line)
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXECUTOR_DIR = Path(__file__).resolve().parent
METEORA_DIR = EXECUTOR_DIR.parent.parent
DISPATCHER = EXECUTOR_DIR / "dispatcher.py"
RUN_PIPELINE = Path("/root/.openclaw-devdash/workspace/run_pipeline.sh")

sys.path.insert(0, str(EXECUTOR_DIR.parent))

from common import paths  # noqa: E402

LIVE_KILL = paths.kill_file()


class DispatcherExitCodeTest(unittest.TestCase):
    def test_exit_killed_constant(self):
        sys.path.insert(0, str(EXECUTOR_DIR))
        import dispatcher
        self.assertEqual(dispatcher.EXIT_KILLED, 75)

    def test_once_with_kill_exits_75(self):
        sys.path.insert(0, str(EXECUTOR_DIR))
        import dispatcher

        with tempfile.TemporaryDirectory() as tmp:
            kill = Path(tmp) / "KILL"
            kill.write_text("halt\n")
            calls = {"run_once": 0, "epilogue": 0}

            orig = (dispatcher.KILL_FILE, dispatcher.load_config,
                    dispatcher.load_rails, dispatcher.run_once,
                    dispatcher._capital_epilogue)
            try:
                dispatcher.KILL_FILE = kill
                dispatcher.load_config = lambda: {"agent": {"mode": "dry_run"}}
                dispatcher.load_rails = lambda: {}

                def fake_run_once(cfg, rails):
                    calls["run_once"] += 1
                    return []

                def fake_epilogue(cfg, executed):
                    calls["epilogue"] += 1
                    return False

                dispatcher.run_once = fake_run_once
                dispatcher._capital_epilogue = fake_epilogue
                sys.argv = ["dispatcher.py", "--once"]
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit) as ctx:
                        dispatcher.main()
                self.assertEqual(ctx.exception.code, 75)
                self.assertIn("KILLED", out.getvalue())
                self.assertIn("75", out.getvalue())
            finally:
                (dispatcher.KILL_FILE, dispatcher.load_config,
                 dispatcher.load_rails, dispatcher.run_once,
                 dispatcher._capital_epilogue) = orig


@unittest.skipUnless(
    RUN_PIPELINE.exists(),
    "run_pipeline.sh is not present on this host (the driver lives outside this checkout)",
)
class DriverKillTest(unittest.TestCase):
    """Real subprocess: the driver must stop before screening."""

    def setUp(self):
        self._created = False
        if LIVE_KILL.exists():
            self.skipTest("KILL already present; refusing to touch owner state")
        self._scan_before = max(
            [p.stat().st_mtime for p in paths.pool_scan_dir()
             .glob("pool_scan-*.json")] or [0])

    def tearDown(self):
        if self._created and LIVE_KILL.exists():
            LIVE_KILL.unlink()

    def test_driver_halts_before_screening(self):
        LIVE_KILL.write_text("test fixture - removed by test tearDown\n")
        self._created = True
        env = dict(os.environ, PIPELINE_WRITE_SIGNALS="1",
                   PIPELINE_START_DISPATCHER="1")
        proc = subprocess.run(["bash", str(RUN_PIPELINE)], env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 75, proc.stderr[-400:])
        self.assertIn("KILLED", proc.stdout)
        self.assertNotIn("Pipeline run complete", proc.stdout)
        self.assertNotIn("Running Missy pool screener", proc.stdout)
        after = max([p.stat().st_mtime for p in
                     paths.pool_scan_dir()
                     .glob("pool_scan-*.json")] or [0])
        self.assertEqual(after, self._scan_before,
                         "a killed cycle must not produce a new pool scan")


if __name__ == "__main__":
    unittest.main()
