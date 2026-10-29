#!/usr/bin/env bash
# Run every test suite in this repo.
#
# The suites live in three places and used to have no single entry point:
#   - the executor's own unit tests (agents/meteora-dlmm/executor/common)
#   - the root integration tests (tests/) — these import the executor by path
#   - the chain layer's JS tests (executor/chain/*.test.js)
#
# The Python suites are hermetic after the 2026-10-29 path refactor: they no
# longer need /data or the live config. Host-dependent tests skip themselves
# rather than fail (run_pipeline.sh, Sheldon's manifest, a live KILL file).
set -euo pipefail
cd "$(dirname "$0")"

EXECUTOR="agents/meteora-dlmm/executor"

echo "== executor unit tests"
(cd "$EXECUTOR" && python3 -m unittest discover -s common -p 'test_*.py')

echo
echo "== root integration tests"
python3 -m unittest discover -s tests -p 'test_*.py'

echo
if command -v node >/dev/null 2>&1; then
  echo "== chain layer (JS)"
  (cd "$EXECUTOR" && node --test chain/*.test.js)
else
  echo "== chain layer (JS): skipped, node not installed"
fi

echo
echo "all suites passed"
