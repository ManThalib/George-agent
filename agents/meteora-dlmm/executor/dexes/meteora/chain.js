/**
 * Meteora DLMM chain handler.
 */

import { Keypair, PublicKey } from "@solana/web3.js";
import { createRequire } from "module";
import { log, respondError } from "../../chain/common.js";
import { asTransactions, signAndSend, simulate, txToBase64 } from "../../chain/tx.js";

const require = createRequire(import.meta.url);
const DLMM = require("@meteora-ag/dlmm");
const StrategyType = DLMM.StrategyType;

function withinOnePct(got, want) {
  // Compare BigInt raw amounts with a 1% tolerance. A zero request must
  // stay zero: any deposit on that side means the wrong token went in.
  if (want === 0n) return got === 0n;
  const diff = got > want ? got - want : want - got;
  return diff * 100n <= want;
}

async function verifyOpen(req, connection, wallet, positionPubKey) {
  try {
    const dlmm = await DLMM.create(connection, new PublicKey(req.pool_address));
    const position = await dlmm.getPosition(positionPubKey);
    const pd = position.positionData || {};
    const lower = Number(req.bin_range?.lower), upper = Number(req.bin_range?.upper);
    // SDK >=1.9 keeps the bin bounds inside positionData (verified live
    // 2026-10-01: top-level lowerBinId/upperBinId are undefined).
    const range_ok = Number(pd.lowerBinId) === lower && Number(pd.upperBinId) === upper;
    const { amount_x = "0", amount_y = "0" } = req.liquidity || {};
    const got_x = BigInt(pd.totalXAmount?.toString() ?? "0");
    const got_y = BigInt(pd.totalYAmount?.toString() ?? "0");
    const amounts_ok = withinOnePct(got_x, BigInt(amount_x || "0"))
      && withinOnePct(got_y, BigInt(amount_y || "0"));
    return {
      ok: range_ok && amounts_ok,
      position_found: true,
      range_ok,
      amounts_ok,
      lower_bin_id: Number(pd.lowerBinId),
      upper_bin_id: Number(pd.upperBinId),
      deposited_x_raw: got_x.toString(),
      deposited_y_raw: got_y.toString(),
    };
  } catch (err) {
    return { ok: false, position_found: false, error: err.message };
  }
}

async function verifyClosed(req, connection, wallet, positionPubKey) {
  try {
    const info = await connection.getAccountInfo(positionPubKey);
    return { ok: info === null, position_closed: info === null };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

export async function openPosition(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const dlmm = await DLMM.create(connection, new PublicKey(poolAddress));

  const { lower, upper } = req.bin_range || {};
  if (lower === undefined || upper === undefined) throw new Error("bin_range required for meteora open");
  const { amount_x = "0", amount_y = "0" } = req.liquidity || {};
  const slippagePct = (req.max_slippage_bps ?? 100) / 100;

  const positionKeypair = Keypair.generate();
  const BN = require("bn.js");
  let tx;
  try {
    tx = await dlmm.initializePositionAndAddLiquidityByStrategy({
      positionPubKey: positionKeypair.publicKey,
      totalXAmount: new BN(amount_x),
      totalYAmount: new BN(amount_y),
      strategy: { minBinId: Number(lower), maxBinId: Number(upper), strategyType: StrategyType.Spot },
      user: wallet.publicKey,
      slippage: slippagePct,
    });
  } catch (err) {
    throw new Error(`meteora open build failed: ${err.message}`);
  }

  const extraSigners = [positionKeypair];
  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim, notes: `positionMint=${positionKeypair.publicKey.toBase58()}` };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, extraSigners);
  const verify = await verifyOpen(req, connection, wallet, positionKeypair.publicKey);
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    position_id: positionKeypair.publicKey.toBase58(),
    verify,
    notes: `positionMint=${positionKeypair.publicKey.toBase58()}`,
  };
}

export async function closePosition(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for meteora close");

  const dlmm = await DLMM.create(connection, new PublicKey(poolAddress));
  const positionPubKey = new PublicKey(positionId);

  let position;
  try {
    position = await dlmm.getPosition(positionPubKey);
  } catch (err) {
    throw new Error(`meteora getPosition failed: ${err.message}`);
  }

  // 1. Remove liquidity from all populated bins.
  const binData = position.positionData?.positionBinData || [];
  const bins = binData.filter((b) => BigInt(b.positionLiquidity || "0") > 0n);
  const feeBins = binData.filter(
    (b) =>
      BigInt(b.positionFeeXAmount || "0") > 0n ||
      BigInt(b.positionFeeYAmount || "0") > 0n ||
      (b.positionRewardAmount || ["0", "0"]).some((r) => BigInt(r || "0") > 0n)
  );
  const BN = require("bn.js");
  let removeTxs = [];
  let claimAndCloseTxs = [];
  if (bins.length > 0) {
    const fromBinId = Math.min(...bins.map((b) => b.binId));
    const toBinId = Math.max(...bins.map((b) => b.binId));
    const removeTx = await dlmm.removeLiquidity({
      user: wallet.publicKey,
      position: positionPubKey,
      fromBinId,
      toBinId,
      bps: new BN(10000),
      shouldClaimAndClose: false,
    });
    removeTxs = asTransactions(removeTx);
  } else if (feeBins.length > 0) {
    // Liquidity fully removed but per-bin fees/rewards remain. The program
    // rejects closePosition2 with NonEmptyPosition until they are claimed.
    // shouldClaimAndClose bundles claim + close into one atomic tx and works
    // with zero liquidity (active bins fall back to fee-bearing bins).
    const fromBinId = Math.min(...binData.map((b) => b.binId));
    const toBinId = Math.max(...binData.map((b) => b.binId));
    const tx = await dlmm.removeLiquidity({
      user: wallet.publicKey,
      position: positionPubKey,
      fromBinId,
      toBinId,
      bps: new BN(10000),
      shouldClaimAndClose: true,
    });
    claimAndCloseTxs = asTransactions(tx);
  }

  if (req.mode === "simulate") {
    const txs = removeTxs.length ? removeTxs : claimAndCloseTxs;
    if (!txs.length) throw new Error("meteora close built no transaction");
    const sims = await Promise.all(txs.map((tx) => simulate(connection, tx)));
    return { tx_base64: txs.map((tx) => txToBase64(tx)), simulation: sims };
  }

  const signatures = [];
  for (const tx of removeTxs) {
    const sent = await signAndSend(connection, tx, wallet, []);
    signatures.push({ step: "removeLiquidity", ...sent });
  }

  if (claimAndCloseTxs.length > 0) {
    for (const tx of claimAndCloseTxs) {
      const sent = await signAndSend(connection, tx, wallet, []);
      signatures.push({ step: "claimAndClose", ...sent });
    }
    const verify = await verifyClosed(req, connection, wallet, positionPubKey);
    return { signatures, close_signature: signatures[signatures.length - 1].signature, verify };
  }

  // 2. Claim any accrued fees. claimSwapFee returns an ARRAY of prepared
  // transactions (and throws "No fee to claim" when none) — send each one.
  // The close below fails with NonEmptyPosition while fees are unclaimed.
  try {
    const fresh = await dlmm.getPosition(positionPubKey);
    const claimTxs = await dlmm.claimSwapFee({ owner: wallet.publicKey, position: fresh });
    for (const tx of claimTxs || []) {
      const sent = await signAndSend(connection, tx, wallet, []);
      signatures.push({ step: "claimSwapFee", ...sent });
    }
  } catch (err) {
    log("meteora claimSwapFee skipped:", err.message);
  }

  // 3. Close the empty position account. closePositionIfEmpty is the
  // program-side safe variant: it closes when the position is empty and
  // becomes a no-op otherwise, so a stale bin read cannot brick the close.
  const final = await dlmm.getPosition(positionPubKey);
  const binsAfterRemove = (final.positionData?.positionBinData || []).filter(
    (b) => BigInt(b.positionLiquidity || "0") > 0n
  );
  const closeTx = binsAfterRemove.length
    ? await dlmm.closePosition({ owner: wallet.publicKey, position: final })
    : await dlmm.closePositionIfEmpty({ owner: wallet.publicKey, position: final });
  if (!closeTx) throw new Error("meteora closePosition returned no transaction");
  const closeSent = await signAndSend(connection, closeTx, wallet, []);
  signatures.push({ step: "closePosition", ...closeSent });

  const verify = await verifyClosed(req, connection, wallet, positionPubKey);
  return { signatures, close_signature: closeSent.signature, verify };
}

export async function claimFees(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for meteora claim");

  const dlmm = await DLMM.create(connection, new PublicKey(poolAddress));
  const positionPubKey = new PublicKey(positionId);
  let position;
  try {
    position = await dlmm.getPosition(positionPubKey);
  } catch (err) {
    throw new Error(`meteora getPosition failed: ${err.message}`);
  }

  const tx = await dlmm.claimSwapFee({ owner: wallet.publicKey, position });
  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, []);
  return { signature, slot, confirmed_via, ...(fallback_broadcast ? { fallback_broadcast } : {}) };
}

function positionTotals(position) {
  const pd = position.positionData || {};
  return {
    x: BigInt(pd.totalXAmount?.toString() ?? "0"),
    y: BigInt(pd.totalYAmount?.toString() ?? "0"),
    liquidity: (pd.positionBinData || []).reduce(
      (acc, b) => acc + BigInt(b.positionLiquidity || "0"),
      0n
    ),
  };
}

async function verifyAdd(req, connection, positionPubKey, before) {
  try {
    const dlmm = await DLMM.create(connection, new PublicKey(req.pool_address));
    const position = await dlmm.getPosition(positionPubKey);
    const after = positionTotals(position);
    const { amount_x = "0", amount_y = "0" } = req.liquidity || {};
    const gotX = after.x > before.x ? after.x - before.x : 0n;
    const gotY = after.y > before.y ? after.y - before.y : 0n;
    const x_ok = withinOnePct(gotX, BigInt(amount_x || "0"));
    const y_ok = withinOnePct(gotY, BigInt(amount_y || "0"));
    return {
      ok: x_ok && y_ok,
      position_found: true,
      before_x_raw: before.x.toString(),
      before_y_raw: before.y.toString(),
      after_x_raw: after.x.toString(),
      after_y_raw: after.y.toString(),
    };
  } catch (err) {
    return { ok: false, position_found: false, error: err.message };
  }
}

async function verifyRemove(req, connection, positionPubKey, before) {
  try {
    const dlmm = await DLMM.create(connection, new PublicKey(req.pool_address));
    const position = await dlmm.getPosition(positionPubKey);
    const after = positionTotals(position);
    // Liquidity must have shrunk and the position account must still exist
    // (a partial remove never closes the account).
    return {
      ok: after.liquidity < before.liquidity,
      position_found: true,
      before_liquidity_raw: before.liquidity.toString(),
      after_liquidity_raw: after.liquidity.toString(),
    };
  } catch (err) {
    return { ok: false, position_found: false, error: err.message };
  }
}

export async function addLiquidity(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for meteora add_liquidity");

  const dlmm = await DLMM.create(connection, new PublicKey(poolAddress));
  const positionPubKey = new PublicKey(positionId);
  let position;
  try {
    position = await dlmm.getPosition(positionPubKey);
  } catch (err) {
    throw new Error(`meteora getPosition failed: ${err.message}`);
  }

  // The signal's bin_range must match the position's on-chain bounds: a
  // stale or wrong-range signal must never add capital to the wrong range.
  const lower = Number(req.bin_range?.lower), upper = Number(req.bin_range?.upper);
  if (!Number.isFinite(lower) || !Number.isFinite(upper)) {
    throw new Error("bin_range required for meteora add_liquidity");
  }
  const posLower = Number(position.positionData?.lowerBinId);
  const posUpper = Number(position.positionData?.upperBinId);
  if (posLower !== lower || posUpper !== upper) {
    throw new Error(
      `meteora add_liquidity bin_range mismatch: signal [${lower}, ${upper}] vs ` +
      `position [${posLower}, ${posUpper}]`
    );
  }

  // Active-bin guard (belt and braces with the Python tick check): adding
  // while price sits outside the range parks the capital in dead bins.
  const activeBin = await dlmm.getActiveBin();
  const activeId = Number(activeBin?.binId);
  if (Number.isFinite(activeId) && (activeId < lower || activeId > upper)) {
    throw new Error(
      `meteora add_liquidity refused: active bin ${activeId} outside position range [${lower}, ${upper}]`
    );
  }

  const { amount_x = "0", amount_y = "0" } = req.liquidity || {};
  const BN = require("bn.js");
  const slippagePct = (req.max_slippage_bps ?? 100) / 100;
  const before = positionTotals(position);

  let tx;
  try {
    tx = await dlmm.addLiquidityByStrategy({
      positionPubKey,
      totalXAmount: new BN(amount_x),
      totalYAmount: new BN(amount_y),
      strategy: { minBinId: lower, maxBinId: upper, strategyType: StrategyType.Spot },
      user: wallet.publicKey,
      slippage: slippagePct,
    });
  } catch (err) {
    throw new Error(`meteora add_liquidity build failed: ${err.message}`);
  }

  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim, notes: `add to position=${positionId}` };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, []);
  const verify = await verifyAdd(req, connection, positionPubKey, before);
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    position_id: positionId,
    verify,
    notes: `add to position=${positionId}`,
  };
}

export async function removeLiquidity(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for meteora remove_liquidity");

  const dlmm = await DLMM.create(connection, new PublicKey(poolAddress));
  const positionPubKey = new PublicKey(positionId);
  let position;
  try {
    position = await dlmm.getPosition(positionPubKey);
  } catch (err) {
    throw new Error(`meteora getPosition failed: ${err.message}`);
  }

  const binData = position.positionData?.positionBinData || [];
  const bins = binData.filter((b) => BigInt(b.positionLiquidity || "0") > 0n);
  if (!bins.length) {
    throw new Error("meteora remove_liquidity: position holds no liquidity; use claim or close");
  }

  const bpsValue = Number(req.bps);
  if (!Number.isInteger(bpsValue) || bpsValue <= 0 || bpsValue >= 10000) {
    throw new Error(`meteora remove_liquidity bps out of range (1-9999): ${req.bps}`);
  }

  const fromBinId = Math.min(...bins.map((b) => b.binId));
  const toBinId = Math.max(...bins.map((b) => b.binId));
  const BN = require("bn.js");
  const before = positionTotals(position);

  let txs;
  try {
    txs = asTransactions(await dlmm.removeLiquidity({
      user: wallet.publicKey,
      position: positionPubKey,
      fromBinId,
      toBinId,
      bps: new BN(bpsValue),
      shouldClaimAndClose: false,
    }));
  } catch (err) {
    throw new Error(`meteora remove_liquidity build failed: ${err.message}`);
  }
  if (!txs.length) throw new Error("meteora remove_liquidity built no transaction");

  if (req.mode === "simulate") {
    const sims = await Promise.all(txs.map((tx) => simulate(connection, tx)));
    return { tx_base64: txs.map((tx) => txToBase64(tx)), simulation: sims, notes: `remove ${bpsValue}bps from position=${positionId}` };
  }

  const signatures = [];
  for (const tx of txs) {
    const sent = await signAndSend(connection, tx, wallet, []);
    signatures.push({ step: "removeLiquidity", ...sent });
  }

  // Optional fee sweep after the removal; failure never undoes the removal.
  if (req.claim_after) {
    try {
      const fresh = await dlmm.getPosition(positionPubKey);
      const claimTxs = await dlmm.claimSwapFee({ owner: wallet.publicKey, position: fresh });
      for (const tx of claimTxs || []) {
        const sent = await signAndSend(connection, tx, wallet, []);
        signatures.push({ step: "claimSwapFee", ...sent });
      }
    } catch (err) {
      log("meteora remove_liquidity claimSwapFee skipped:", err.message);
    }
  }

  const verify = await verifyRemove(req, connection, positionPubKey, before);
  return {
    signatures,
    signature: signatures[0].signature,
    position_id: positionId,
    verify,
    notes: `remove ${bpsValue}bps from position=${positionId}`,
  };
}
