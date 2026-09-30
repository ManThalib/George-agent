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
    const range_ok = Number(position.lowerBinId) === lower && Number(position.upperBinId) === upper;
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
      lower_bin_id: Number(position.lowerBinId),
      upper_bin_id: Number(position.upperBinId),
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
