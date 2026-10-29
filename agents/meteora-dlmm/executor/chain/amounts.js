/**
 * Shared amount / tick helpers for the DEX handlers.
 *
 * These were duplicated per handler, and the copies disagreed in ways their
 * names did not reveal:
 *
 *   - `withinOnePct` meant a 10% band in meteora/chain.js (live 2026-10-16: a
 *     CORRECT open deposited x +3.85% / y -3.85% and the 1% band rejected it)
 *     but 1% elsewhere;
 *   - raydium and orca each carried their own identical `withinMax`;
 *   - `alignTick` was written out three times.
 *
 * Each function here says what it actually does. Callers pass their own band
 * explicitly, so the three DEXes keep their current tolerances exactly.
 */

/** Round a tick onto the pool's tick grid. */
export function alignTick(tick, tickSpacing) {
  const spacing = Number(tickSpacing) || 1;
  return Math.round(Number(tick) / spacing) * spacing;
}

/**
 * Deposit-MAX check used by the CLMM verifiers: the pool takes what the range
 * needs, so a deposit up to +2% of the requested max is a success. A zero
 * request must stay zero.
 */
export function withinDepositMax(got, want) {
  if (want === 0n) return got === 0n;
  return got * 100n <= want * 102n;
}

/**
 * Symmetric delta check: |got - want| within `bandPct` percent of `want`.
 *
 * A zero request must stay zero — any deposit on that side means the wrong
 * token went in — so the band only applies when both sides are positive.
 */
export function deltaWithinBand(got, want, bandPct) {
  if (got > 0n && want > 0n) {
    const diff = got > want ? got - want : want - got;
    return diff * 100n <= want * bandPct;
  }
  return got === want;
}

/** Meteora keeps the 10% band on a correct, price-moved open. */
export const METEORA_AMOUNTS_BAND_PCT = 10n;
