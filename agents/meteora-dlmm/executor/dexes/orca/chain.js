/**
 * Orca Whirlpool chain handler.
 */

import { PublicKey } from "@solana/web3.js";
import { buildWhirlpoolClient, ORCA_WHIRLPOOL_PROGRAM_ID, WhirlpoolContext, PriceMath } from "@orca-so/whirlpools-sdk";
import BN from "bn.js";
import Decimal from "decimal.js";
import { signAndSend, simulate, txToBase64 } from "../../chain/tx.js";

function walletStub(publicKey) {
  return {
    publicKey: new PublicKey(publicKey),
    signTransaction: () => { throw new Error("signTransaction not available during instruction build"); },
    signAllTransactions: () => { throw new Error("signAllTransactions not available during instruction build"); },
  };
}

function makeClient(connection, publicKey) {
  const wallet = walletStub(publicKey);
  const ctx = WhirlpoolContext.from(connection, wallet, undefined, undefined, undefined, ORCA_WHIRLPOOL_PROGRAM_ID);
  return buildWhirlpoolClient(ctx);
}

async function buildPayload(builder) {
  const payload = await builder.build();
  return { tx: payload.transaction, signers: payload.signers ?? [] };
}

function withinOnePct(got, want) {
  if (want === 0n) return got === 0n;
  const diff = got > want ? got - want : want - got;
  return diff * 100n <= want;
}

const Q64 = new Decimal(2).pow(64);

function amountsForLiquidity(liquidityBn, tickLower, tickUpper, tickCurrent) {
  // Standard CLMM math on X64 sqrt prices. Returns raw integer strings.
  const sqrtL = new Decimal(PriceMath.tickIndexToSqrtPriceX64(tickLower).toString());
  const sqrtU = new Decimal(PriceMath.tickIndexToSqrtPriceX64(tickUpper).toString());
  const sqrtC = new Decimal(PriceMath.tickIndexToSqrtPriceX64(tickCurrent).toString());
  const L = new Decimal(liquidityBn.toString());
  const amountX = L.mul(sqrtU.minus(Decimal.min(sqrtC, sqrtU))).div(Q64);
  const amountY = L.mul(Decimal.min(sqrtC, sqrtU).minus(sqrtL)).div(Q64);
  return { amountX: amountX.toFixed(0), amountY: amountY.toFixed(0) };
}

async function clientGetPosition(connection, wallet, address) {
  const client = makeClient(connection, wallet.publicKey);
  return client.getPosition(address);
}

async function verifyOpen(req, connection, wallet, positionMint, pool) {
  try {
    const positionPda = PublicKey.findProgramAddressSync(
      [Buffer.from("position"), positionMint.toBuffer()],
      ORCA_WHIRLPOOL_PROGRAM_ID
    )[0];
    const position = await clientGetPosition(connection, wallet, positionPda);
    const data = position.getData();
    const tickLower = Number(data.tickLowerIndex);
    const tickUpper = Number(data.tickUpperIndex);
    const expectedLower = Math.round(Number(req.bin_range?.lower ?? NaN));
    const expectedUpper = Math.round(Number(req.bin_range?.upper ?? NaN));
    const range_ok = tickLower === expectedLower && tickUpper === expectedUpper;
    const liquidity = new BN(data.liquidity.toString());
    const { amount_x = "0", amount_y = "0" } = req.liquidity || {};
    let amounts_ok = true;
    let derived = {};
    try {
      const tickCurrent = Number(pool.getData().tickCurrentIndex);
      const { amountX, amountY } = amountsForLiquidity(liquidity, tickLower, tickUpper, tickCurrent);
      amounts_ok = withinOnePct(BigInt(amountX), BigInt(amount_x || "0"))
        && withinOnePct(BigInt(amountY), BigInt(amount_y || "0"));
      derived = { derived_x_raw: amountX, derived_y_raw: amountY };
    } catch (err) {
      derived = { derived_error: err.message };
    }
    return {
      ok: range_ok && amounts_ok,
      position_found: true,
      range_ok,
      amounts_ok,
      tick_lower_index: tickLower,
      tick_upper_index: tickUpper,
      liquidity: liquidity.toString(),
      ...derived,
    };
  } catch (err) {
    return { ok: false, position_found: false, error: err.message };
  }
}

async function verifyClosed(req, connection, wallet, positionId) {
  try {
    await clientGetPosition(connection, wallet, new PublicKey(positionId));
    return { ok: false, position_closed: false };
  } catch (err) {
    // Account gone: the close landed. Account-not-found errors surface in
    // different shapes (SDK wrap, RPC); treat any as closed.
    const msg = String(err?.message ?? err);
    if (/not found|does not exist|AccountNotFound/i.test(msg)) {
      return { ok: true, position_closed: true };
    }
    return { ok: false, error: msg };
  }
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
  const { positionMint, tx: builder } = await pool.openPosition(tickLower, tickUpper, input, wallet.publicKey);

  const positionMintB58 = positionMint.toBase58();
  const positionPda = PublicKey.findProgramAddressSync(
    [Buffer.from("position"), positionMint.toBuffer()],
    ORCA_WHIRLPOOL_PROGRAM_ID
  )[0];

  const { tx, signers } = await buildPayload(builder);
  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim, notes: `positionMint=${positionMintB58} pda=${positionPda.toBase58()}` };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, signers);
  const verify = await verifyOpen(req, connection, wallet, positionMint, pool);
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    position_id: positionPda.toBase58(),
    verify,
    notes: `positionMint=${positionMintB58} pda=${positionPda.toBase58()}`,
  };
}

export async function closePosition(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for orca close");
  const slippageBps = req.max_slippage_bps ?? 100;
  const slippage = { numerator: new BN(slippageBps), denominator: new BN(10000) };

  const client = makeClient(connection, wallet.publicKey);
  const pool = await client.getPool(new PublicKey(poolAddress));
  const builders = await pool.closePosition(new PublicKey(positionId), slippage, wallet.publicKey);
  const payloads = await Promise.all(builders.map(buildPayload));

  if (req.mode === "simulate") {
    const sims = await Promise.all(payloads.map((p) => simulate(connection, p.tx)));
    return { tx_base64: payloads.map((p) => txToBase64(p.tx)), simulation: sims };
  }

  const signatures = [];
  for (const p of payloads) {
    const sent = await signAndSend(connection, p.tx, wallet, p.signers);
    signatures.push(sent);
  }
  const verify = await verifyClosed(req, connection, wallet, positionId);
  return { signatures, verify };
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
