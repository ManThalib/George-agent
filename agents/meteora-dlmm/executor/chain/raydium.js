/**
 * Raydium CLMM handler.
 */

import { PublicKey } from "@solana/web3.js";
import { Raydium, TxVersion, CLMM_PROGRAM_ID, PersonalPositionLayout } from "@raydium-io/raydium-sdk-v2";
import BN from "bn.js";
import { signAndSend, simulate, txToBase64 } from "./tx.js";

let raydiumCache = null;

async function getRaydium(connection, publicKey) {
  if (raydiumCache) return raydiumCache;
  raydiumCache = await Raydium.load({
    connection,
    owner: publicKey,
    cluster: "mainnet",
    disableFeatureCheck: true,
  });
  return raydiumCache;
}

async function fetchPositionAccount(connection, positionId) {
  const account = await connection.getAccountInfo(new PublicKey(positionId));
  if (!account) throw new Error(`raydium position not found: ${positionId}`);
  if (!account.owner.equals(CLMM_PROGRAM_ID)) {
    throw new Error(`raydium position not found: ${positionId} (unexpected owner ${account.owner.toBase58()})`);
  }
  try {
    return PersonalPositionLayout.decode(account.data);
  } catch (err) {
    throw new Error(`raydium position decode failed for ${positionId}: ${err.message}`);
  }
}

export async function openPosition(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const { lower, upper } = req.bin_range || {};
  if (lower === undefined || upper === undefined) throw new Error("bin_range required for raydium open");
  const { amount_x = "0", amount_y = "0" } = req.liquidity || {};

  const raydium = await getRaydium(connection, wallet.publicKey);
  const { poolInfo, poolKeys } = await raydium.clmm.getPoolInfoFromRpc(poolAddress);

  const tickSpacing = Number(poolInfo.tickSpacing) || 1;
  const alignTick = (t) => Math.round(Number(t) / tickSpacing) * tickSpacing;
  const tickLower = alignTick(lower);
  const tickUpper = alignTick(upper);
  if (tickLower >= tickUpper) throw new Error(`raydium open invalid bin_range: ${tickLower} >= ${tickUpper}`);

  const res = await raydium.clmm.openPositionFromLiquidity({
    poolInfo,
    poolKeys,
    ownerInfo: { useSOLBalance: true },
    tickLower,
    tickUpper,
    amountMaxA: new BN(amount_x),
    amountMaxB: new BN(amount_y),
    base: "MintA",
    txVersion: TxVersion.LEGACY,
  });

  const tx = res.transaction;
  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return {
      tx_base64: [txToBase64(tx)],
      simulation: sim,
      notes: `extInfo=${JSON.stringify(Object.keys(res.extInfo))}`,
    };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, res.signers);
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    position_id: res.extInfo?.nftMint?.toBase58?.() ?? null,
  };
}

export async function closePosition(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for raydium close");

  const raydium = await getRaydium(connection, wallet.publicKey);
  const { poolInfo, poolKeys } = await raydium.clmm.getPoolInfoFromRpc(poolAddress);

  const ownerPosition = await fetchPositionAccount(connection, positionId);

  const res = await raydium.clmm.closePosition({
    poolInfo,
    poolKeys,
    ownerPosition,
    txVersion: TxVersion.LEGACY,
  });

  const tx = res.transaction;
  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, res.signers);
  return { signature, slot, confirmed_via, ...(fallback_broadcast ? { fallback_broadcast } : {}) };
}

export async function claimFees(req, connection, wallet) {
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for raydium claim");

  const raydium = await getRaydium(connection, wallet.publicKey);
  const ownerPosition = await fetchPositionAccount(connection, positionId);

  const res = await raydium.clmm.harvestAllRewards({
    allPoolInfo: [],
    allPositions: [ownerPosition],
    ownerInfo: { useSOLBalance: true },
    txVersion: TxVersion.LEGACY,
  });

  // harvestAllRewards returns multiple transactions
  const txs = res.transactions;
  if (req.mode === "simulate") {
    const sims = await Promise.all(txs.map((tx) => simulate(connection, tx)));
    return { tx_base64: txs.map(txToBase64), simulation: sims };
  }

  const signatures = [];
  const confirmations = [];
  for (const tx of txs) {
    const sent = await signAndSend(connection, tx, wallet);
    const { signature, confirmed_via, fallback_broadcast } = sent;
    signatures.push(signature);
    confirmations.push({ signature, confirmed_via, ...(fallback_broadcast ? { fallback_broadcast } : {}) });
  }
  return { signatures, confirmations };
}
