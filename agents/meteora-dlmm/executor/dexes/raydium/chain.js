/**
 * Raydium CLMM chain handler.
 */

import { PublicKey } from "@solana/web3.js";
import {
  Raydium,
  TxVersion,
  CLMM_PROGRAM_ID,
  PersonalPositionLayout,
  TickUtil,
  LiquidityMathUtil,
  POSITION_SEED,
} from "@raydium-io/raydium-sdk-v2";
import BN from "bn.js";
import { signAndSend, simulate, txToBase64 } from "../../chain/tx.js";

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
  // position_id is the position NFT mint (what openPosition returns); the
  // personal-position account is its PDA. Accept the account address directly
  // when given, else derive the PDA from the mint.
  let address = new PublicKey(positionId);
  let account = await connection.getAccountInfo(address);
  if (!account || !account.owner.equals(CLMM_PROGRAM_ID)) {
    const [pda] = await PublicKey.findProgramAddress(
      [POSITION_SEED, address.toBuffer()],
      CLMM_PROGRAM_ID,
    );
    const pdaAccount = await connection.getAccountInfo(pda);
    if (!pdaAccount) throw new Error(`raydium position not found: ${positionId}`);
    if (!pdaAccount.owner.equals(CLMM_PROGRAM_ID)) {
      throw new Error(`raydium position not found: ${positionId} (unexpected owner ${pdaAccount.owner.toBase58()})`);
    }
    address = pda;
    account = pdaAccount;
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

  // amount_x/amount_y are deposit amounts; openPositionFromLiquidity needs the
  // position liquidity itself. Omitting it encoded liquidity=0: the position
  // opened with a minted NFT but no deposit, and both simulation and the
  // on-chain program succeeded (seen live 2026-09-30, pool 3ucNos4N...).
  const tickCurrent = Number(poolInfo.tickCurrent);
  const liquidity = LiquidityMathUtil.getLiquidityFromAmounts(
    TickUtil.getSqrtPriceAtTick(tickCurrent),
    TickUtil.getSqrtPriceAtTick(tickLower),
    TickUtil.getSqrtPriceAtTick(tickUpper),
    new BN(amount_x),
    new BN(amount_y),
  );
  if (liquidity.lte(new BN(0))) {
    throw new Error(
      `raydium open computed zero liquidity from amounts x=${amount_x} y=${amount_y} range [${tickLower}, ${tickUpper}] current=${tickCurrent}`,
    );
  }

  // The program recomputes required amounts at the live tick and rejects the
  // open when they exceed the maxes (PriceSlippageCheck, error 6017 — seen
  // live 2026-09-30). Pad the maxes by the slippage rail (≤1%) to absorb
  // drift between this snapshot and execution.
  const { amountA, amountB } = LiquidityMathUtil.getAmountsForLiquidity(
    TickUtil.getSqrtPriceAtTick(tickCurrent),
    TickUtil.getSqrtPriceAtTick(tickLower),
    TickUtil.getSqrtPriceAtTick(tickUpper),
    liquidity,
    true,
  );
  const slipBps = Math.min(Number(req.max_slippage_bps ?? 100), 100);
  const BPS = new BN(10000);
  const amountMaxA = amountA.mul(BPS.addn(slipBps)).div(BPS);
  const amountMaxB = amountB.mul(BPS.addn(slipBps)).div(BPS);

  const res = await raydium.clmm.openPositionFromLiquidity({
    poolInfo,
    poolKeys,
    ownerInfo: { useSOLBalance: true },
    tickLower,
    tickUpper,
    liquidity,
    amountMaxA,
    amountMaxB,
    txVersion: TxVersion.LEGACY,
  });

  const tx = res.transaction;
  const nftMint = res.extInfo?.address?.nftMint ?? res.extInfo?.nftMint;
  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim, notes: `extInfo=${JSON.stringify(Object.keys(res.extInfo))}`, position_id: nftMint?.toBase58?.() ?? null };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, res.signers);
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    position_id: nftMint?.toBase58?.() ?? null,
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

  const txs = res.transactions;
  if (req.mode === "simulate") {
    const sims = await Promise.all(txs.map((tx) => simulate(connection, tx)));
    return { tx_base64: txs.map(txToBase64), simulation: sims };
  }

  const signatures = [];
  for (const tx of txs) {
    const sent = await signAndSend(connection, tx, wallet);
    signatures.push(sent);
  }
  return { signatures };
}
