/**
 * Meteora DLMM handler.
 */

import { Keypair, PublicKey } from "@solana/web3.js";
import { createRequire } from "module";
import BN from "bn.js";
import { log, respondError } from "./common.js";
import { asTransactions, signAndSend, simulate, txToBase64 } from "./tx.js";

// The Meteora ESM build imports a directory from @coral-xyz/anchor which
// Node rejects. The CJS build exposes the DLMM class as module.exports and
// StrategyType as a static property, so load it via createRequire.
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
  const mode = req.mode;
  if (mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [require("./tx.js").txToBase64(tx)], simulation: sim, notes: `positionMint=${positionKeypair.publicKey.toBase58()}` };
  }

  const { signature, slot } = await signAndSend(connection, tx, wallet, extraSigners);
  return {
    signature,
    slot,
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

  // Fetch position account (LbPosition) required by SDK close helpers.
  let position;
  try {
    position = await dlmm.getPosition(positionPubKey);
  } catch (err) {
    throw new Error(`meteora getPosition failed: ${err.message}`);
  }

  // closePosition returns a single Transaction; it claims fees and closes.
  let tx;
  try {
    tx = await dlmm.closePosition({ owner: wallet.publicKey, position });
  } catch (err) {
    throw new Error(`meteora close build failed: ${err.message}`);
  }

  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [require("./tx.js").txToBase64(tx)], simulation: sim };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, []);
  return { signature, slot, confirmed_via, ...(fallback_broadcast ? { fallback_broadcast } : {}) };
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

  let tx;
  try {
    tx = await dlmm.claimSwapFee({ owner: wallet.publicKey, position });
  } catch (err) {
    throw new Error(`meteora claim build failed: ${err.message}`);
  }

  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [require("./tx.js").txToBase64(tx)], simulation: sim };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, []);
  return { signature, slot, confirmed_via, ...(fallback_broadcast ? { fallback_broadcast } : {}) };
}
