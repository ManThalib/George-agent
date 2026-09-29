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
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    position_id: positionKeypair.publicKey.toBase58(),
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
  const bins = (position.positionData?.positionBinData || []).filter((b) => BigInt(b.positionLiquidity || "0") > 0n);
  const signatures = [];
  if (bins.length > 0) {
    const fromBinId = Math.min(...bins.map((b) => b.binId));
    const toBinId = Math.max(...bins.map((b) => b.binId));
    const BN = require("bn.js");
    const removeTx = await dlmm.removeLiquidity({
      user: wallet.publicKey,
      position: positionPubKey,
      fromBinId,
      toBinId,
      bps: new BN(10000),
      shouldClaimAndClose: false,
    });
    const removeTxs = asTransactions(removeTx);
    for (const tx of removeTxs) {
      const sent = await signAndSend(connection, tx, wallet, []);
      signatures.push({ step: "removeLiquidity", ...sent });
    }
  }

  // 2. Claim any accrued fees.
  let claimSent = null;
  try {
    const fresh = await dlmm.getPosition(positionPubKey);
    const claimTx = await dlmm.claimSwapFee({ owner: wallet.publicKey, position: fresh });
    if (claimTx) {
      const sent = await signAndSend(connection, claimTx, wallet, []);
      claimSent = { step: "claimSwapFee", ...sent };
      signatures.push(claimSent);
    }
  } catch (err) {
    log("meteora claimSwapFee skipped:", err.message);
  }

  // 3. Close the empty position account.
  const final = await dlmm.getPosition(positionPubKey);
  const closeTx = await dlmm.closePosition({ owner: wallet.publicKey, position: final });
  if (!closeTx) throw new Error("meteora closePosition returned no transaction");
  const closeSent = await signAndSend(connection, closeTx, wallet, []);
  signatures.push({ step: "closePosition", ...closeSent });

  return { signatures, close_signature: closeSent.signature };
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
