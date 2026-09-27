/**
 * Orca Whirlpool handler.
 */

import { PublicKey } from "@solana/web3.js";
import { buildWhirlpoolClient, ORCA_WHIRLPOOL_PROGRAM_ID, WhirlpoolContext } from "@orca-so/whirlpools-sdk";
import BN from "bn.js";
import Decimal from "decimal.js";
import { signAndSend, simulate, txToBase64 } from "./tx.js";

function walletStub(publicKey) {
  return {
    publicKey: new PublicKey(publicKey),
    signTransaction: () => {
      throw new Error("signTransaction not available during instruction build");
    },
    signAllTransactions: () => {
      throw new Error("signAllTransactions not available during instruction build");
    },
  };
}

function makeClient(connection, publicKey) {
  const wallet = walletStub(publicKey);
  // Signature: from(connection, wallet, fetcher?, lookupTableFetcher?, opts?, programId?)
  const ctx = WhirlpoolContext.from(connection, wallet, undefined, undefined, undefined, ORCA_WHIRLPOOL_PROGRAM_ID);
  return buildWhirlpoolClient(ctx);
}

async function buildPayload(builder) {
  const payload = await builder.build();
  return { tx: payload.transaction, signers: payload.signers ?? [] };
}

function liquidityInputForOpen(pool, slippageBps, amountX, amountY) {
  const data = pool.getData();
  const sqrtPrice = new Decimal(data.sqrtPrice.toString());
  const factor = new Decimal(slippageBps).div(10000);
  const minSqrtPrice = new BN(sqrtPrice.mul(new Decimal(1).minus(factor)).toFixed(0));
  const maxSqrtPrice = new BN(sqrtPrice.mul(new Decimal(1).plus(factor)).toFixed(0));
  return {
    tokenMaxA: new BN(amountX),
    tokenMaxB: new BN(amountY),
    minSqrtPrice,
    maxSqrtPrice,
  };
}

export async function openPosition(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const { lower, upper } = req.bin_range || {};
  if (lower === undefined || upper === undefined) throw new Error("bin_range required for orca open");
  const { amount_x = "0", amount_y = "0" } = req.liquidity || {};
  const slippageBps = req.max_slippage_bps ?? 100;

  const client = makeClient(connection, wallet.publicKey);
  const pool = await client.getPool(new PublicKey(poolAddress));
  const tickSpacing = Number(pool.getData().tickSpacing);
  const alignTick = (t) => Math.round(Number(t) / tickSpacing) * tickSpacing;
  const tickLower = alignTick(lower);
  const tickUpper = alignTick(upper);
  if (tickLower >= tickUpper) throw new Error(`orca open invalid bin_range: ${tickLower} >= ${tickUpper}`);

  const input = liquidityInputForOpen(pool, slippageBps, amount_x, amount_y);

  const { positionMint, tx: builder } = await pool.openPosition(
    tickLower,
    tickUpper,
    input,
    wallet.publicKey
  );

  const { tx, signers } = await buildPayload(builder);
  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim, notes: `positionMint=${positionMint.toBase58()}` };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, signers);
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    position_id: positionMint.toBase58(),
    notes: `positionMint=${positionMint.toBase58()}`,
  };
}

export async function closePosition(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for orca close");
  const slippageBps = req.max_slippage_bps ?? 100;
  // Orca Percentage type: { numerator: bigint, denominator: bigint }
  const slippage = { numerator: BigInt(slippageBps), denominator: BigInt(10000) };

  const client = makeClient(connection, wallet.publicKey);
  const pool = await client.getPool(new PublicKey(poolAddress));
  const builders = await pool.closePosition(new PublicKey(positionId), slippage, wallet.publicKey);
  const payloads = await Promise.all(builders.map(buildPayload));

  if (req.mode === "simulate") {
    const sims = await Promise.all(payloads.map((p) => simulate(connection, p.tx)));
    return { tx_base64: payloads.map((p) => txToBase64(p.tx)), simulation: sims };
  }

  const signatures = [];
  const confirmations = [];
  for (const p of payloads) {
    const sent = await signAndSend(connection, p.tx, wallet, p.signers);
    const { signature, confirmed_via, fallback_broadcast } = sent;
    signatures.push(signature);
    confirmations.push({ signature, confirmed_via, ...(fallback_broadcast ? { fallback_broadcast } : {}) });
  }
  return { signatures, confirmations };
}

export async function claimFees(req, connection, wallet) {
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for orca claim");

  const client = makeClient(connection, wallet.publicKey);
  const position = await client.getPosition(new PublicKey(positionId));
  const builder = await position.collectFees();
  const { tx, signers } = await buildPayload(builder);

  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, signers);
  return { signature, slot, confirmed_via, ...(fallback_broadcast ? { fallback_broadcast } : {}) };
}
