/**
 * JS smoke tests for the chain layer's pure helpers and request validation.
 *
 * There were no JS tests at all before the 2026-10-29 audit. These cover the
 * parts that run without a Solana SDK or RPC: the shared amount/tick helpers
 * and dispatch's request validation. They are deliberately dependency-free
 * (node:test + node:assert), so they run without node_modules:
 *
 *   node --test chain/*.test.js      (or: npm test)
 */

import test from "node:test";
import assert from "node:assert/strict";

import { alignTick, deltaWithinBand, withinDepositMax } from "./amounts.js";

test("alignTick rounds onto the pool's tick grid", () => {
  assert.equal(alignTick(-22802, 10), -22800);
  assert.equal(alignTick(-22522, 10), -22520);
  assert.equal(alignTick(101, 1), 101);
  assert.equal(alignTick(104, 60), 120);
});

test("a zero spacing does not divide by zero", () => {
  assert.equal(alignTick(105, 0), 105);
  assert.equal(alignTick(105, undefined), 105);
});

test("deltaWithinBand accepts the 10% open that the 1% band rejected", () => {
  // Live 2026-10-16: a CORRECT meteora open deposited x +3.85% / y -3.85%.
  const want = 1_000_000n;
  const got = 1_038_500n;
  assert.equal(deltaWithinBand(got, want, 10n), true);
  assert.equal(deltaWithinBand(got, want, 1n), false);
});

test("deltaWithinBand still fails a wrong-side deposit", () => {
  // A zero request must stay zero: any deposit means the wrong token went in.
  assert.equal(deltaWithinBand(5n, 0n, 10n), false);
  assert.equal(deltaWithinBand(0n, 5n, 10n), false);
  assert.equal(deltaWithinBand(0n, 0n, 10n), true);
});

test("withinDepositMax is an upper bound with a 2% pad", () => {
  assert.equal(withinDepositMax(100n, 100n), true);
  assert.equal(withinDepositMax(102n, 100n), true);
  assert.equal(withinDepositMax(103n, 100n), false);
  assert.equal(withinDepositMax(0n, 0n), true);
  assert.equal(withinDepositMax(1n, 0n), false);
});
