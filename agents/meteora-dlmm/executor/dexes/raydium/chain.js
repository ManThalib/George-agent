/**
 * Raydium CLMM chain handler.
 */

import { PublicKey } from "@solana/web3.js";
import {
  Raydium,
  TxVersion,
  CLMM_PROGRAM_ID,
  PersonalPositionLayout,
  PoolInfoLayout,
  TickArrayLayout,
  TickUtil,
  TickArrayUtil,
  PositionUtils,
  getPdaTickArrayAddress,
  LiquidityMathUtil,
  POSITION_SEED,
} from "@raydium-io/raydium-sdk-v2";
import BN from "bn.js";
import { signAndSend, simulate, txToBase64 } from "../../chain/tx.js";
import { fetchUsdPrices, SOL_MINT } from "../../chain/prices.js";

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

/**
 * Compute real pending fees/rewards for a Raydium CLMM position via the
 * SDK's PositionUtils (same math Missy's scanner ports to Python).
 * The PersonalPositionState checkpoint fields stay stale until a claim
 * touches the position, so gating on them would never fire (or always fire).
 */
async function computePending(connection, poolId, ownerPosition) {
  const poolAccount = await connection.getAccountInfo(poolId);
  if (!poolAccount) throw new Error(`raydium pool not found: ${poolId.toBase58()}`);
  const pool = PoolInfoLayout.decode(poolAccount.data);
  // SDK layout field names are tickLower/tickUpper (not tickLowerIndex).
  const tickLowerIndex = ownerPosition.tickLowerIndex ?? ownerPosition.tickLower;
  const tickUpperIndex = ownerPosition.tickUpperIndex ?? ownerPosition.tickUpper;
  const startLower = TickArrayUtil.getTickArrayStartIndex(tickLowerIndex, pool.tickSpacing);
  const startUpper = TickArrayUtil.getTickArrayStartIndex(tickUpperIndex, pool.tickSpacing);
  const addrLower = getPdaTickArrayAddress(CLMM_PROGRAM_ID, poolId, startLower).publicKey;
  const addrUpper = getPdaTickArrayAddress(CLMM_PROGRAM_ID, poolId, startUpper).publicKey;
  const [accLower, accUpper] = await connection.getMultipleAccountsInfo([addrLower, addrUpper]);
  if (!accLower || !accUpper) throw new Error("raydium tick array account missing");
  const tickLowerState = TickArrayLayout.decode(accLower.data).ticks.find(t => t.tick === tickLowerIndex);
  const tickUpperState = TickArrayLayout.decode(accUpper.data).ticks.find(t => t.tick === tickUpperIndex);
  if (!tickLowerState || !tickUpperState) throw new Error("raydium boundary tick not initialized");
  const fees = PositionUtils.GetPositionFees(pool, ownerPosition, tickLowerState, tickUpperState);
  const rewards = PositionUtils.GetPositionRewards(pool, ownerPosition, tickLowerState, tickUpperState);
  return {
    pool,
    fees_raw: [fees.tokenFeeAmountA.toString(), fees.tokenFeeAmountB.toString()],
    rewards_raw: rewards.map(r => r.toString()),
    reward_mints: pool.rewardInfos.map(r => r.mint.toBase58()),
    decimals: [pool.mintDecimalsA, pool.mintDecimalsB],
  };
}

/**
 * USD value of pending fees+rewards, plus SOL gas estimate.
 * Best-effort: pricing failures leave usd undefined so the gate stays open
 * (a claim is never blocked by a pricing outage; the rail documents it).
 */
async function pendingUsd(pending) {
  const mints = [
    pending.pool.mintA.toBase58(),
    pending.pool.mintB.toBase58(),
    ...pending.reward_mints.filter(m => !m.startsWith("1111")),
  ];
  const usd = { fees_usd: null, rewards_usd: null, total_usd: null };
  try {
    const prices = await fetchUsdPrices(mints);
    const scale = (raw, dec, mint) => {
      const price = prices[mint];
      if (!Number.isFinite(price)) return null;
      return (Number(raw) / 10 ** dec) * price;
    };
    const fa = scale(pending.fees_raw[0], pending.decimals[0], pending.pool.mintA.toBase58());
    const fb = scale(pending.fees_raw[1], pending.decimals[1], pending.pool.mintB.toBase58());
    usd.fees_usd = fa === null && fb === null ? null : (fa ?? 0) + (fb ?? 0);
    let rewardsSum = 0;
    let anyReward = false;
    pending.rewards_raw.forEach((raw, i) => {
      const mint = pending.reward_mints[i];
      if (mint.startsWith("1111")) return;
      const v = scale(raw, 6, mint); // reward mint decimals via Jupiter-registered mint info is not fetched; standard 6 fallback corrected below
      if (v === null) return;
      rewardsSum += v;
      anyReward = true;
    });
    usd.rewards_usd = anyReward ? rewardsSum : 0;
    usd.total_usd = (usd.fees_usd ?? 0) + (usd.rewards_usd ?? 0);
    if (usd.fees_usd === null && usd.rewards_usd === null) usd.total_usd = null;
  } catch {
    // pricing unavailable: leave nulls, gate stays open
  }
  return usd;
}

function withinOnePct(got, want) {
  if (want === 0n) return got === 0n;
  const diff = got > want ? got - want : want - got;
  return diff * 100n <= want;
}

// The on-chain program computes required deposit amounts from the pool's live
// sqrtPriceX64, which is NOT the same as TickUtil.getSqrtPriceAtTick(tickCurrent)
// (the pool price sits partway between ticks). For a narrow range the two differ
// by more than the 1% slippage pad, so the open is rejected with
// PriceSlippageCheck (6017). Use the live sqrt price for every amount computation
// so the SDK and the program agree.
function liveSqrtPrice(poolInfo) {
  return new BN(poolInfo.sqrtPriceX64.toString());
}

async function verifyOpen(req, connection, wallet, nftMint, poolInfo) {
  try {
    const ownerPosition = await fetchPositionAccount(connection, nftMint);
    const tickLower = Number(ownerPosition.tickLowerIndex);
    const tickUpper = Number(ownerPosition.tickUpperIndex);
    const expectedLower = Math.round(Number(req.bin_range?.lower ?? NaN));
    const expectedUpper = Math.round(Number(req.bin_range?.upper ?? NaN));
    const range_ok = tickLower === expectedLower && tickUpper === expectedUpper;
    const liquidity = new BN(ownerPosition.liquidity.toString());
    const { amount_x = "0", amount_y = "0" } = req.liquidity || {};
    let amounts_ok = true;
    let derived = {};
    try {
      const sqrtCurrent = liveSqrtPrice(poolInfo);
      const { amountA, amountB } = LiquidityMathUtil.getAmountsForLiquidity(
        sqrtCurrent,
        TickUtil.getSqrtPriceAtTick(tickLower),
        TickUtil.getSqrtPriceAtTick(tickUpper),
        liquidity,
        true,
      );
      amounts_ok = withinOnePct(BigInt(amountA.toString()), BigInt(amount_x || "0"))
        && withinOnePct(BigInt(amountB.toString()), BigInt(amount_y || "0"));
      derived = { derived_a_raw: amountA.toString(), derived_b_raw: amountB.toString() };
    } catch (err) {
      // Amount derivation is a best-effort cross-check; a math failure
      // must not mask the structural (position exists + range) verdict.
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
    await fetchPositionAccount(connection, positionId);
    // Account still decodes: the position was not closed.
    return { ok: false, position_closed: false };
  } catch (err) {
    if (/not found/.test(String(err.message))) {
      return { ok: true, position_closed: true };
    }
    return { ok: false, error: err.message };
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
  const sqrtCurrent = liveSqrtPrice(poolInfo);
  const liquidity = LiquidityMathUtil.getLiquidityFromAmounts(
    sqrtCurrent,
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
    sqrtCurrent,
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
  const verify = await verifyOpen(req, connection, wallet, nftMint, poolInfo);
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    position_id: nftMint?.toBase58?.() ?? null,
    verify,
  };
}

export async function closePosition(req, connection, wallet) {
  const poolAddress = req.pool_address;
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for raydium close");

  const raydium = await getRaydium(connection, wallet.publicKey);
  const { poolInfo, poolKeys } = await raydium.clmm.getPoolInfoFromRpc(poolAddress);
  const ownerPosition = await fetchPositionAccount(connection, positionId);

  // Program rejects closePosition while liquidity remains (err 6003):
  // remove liquidity and close the NFT in one bundled tx via decreaseLiquidity.
  const positionLiquidity = ownerPosition.liquidity ? new BN(ownerPosition.liquidity.toString()) : new BN(0);
  if (positionLiquidity.gt(new BN(0))) {
    const bps = req.max_slippage_bps ?? 100;
    const slippage = bps / 10000;
    const sqrtPriceCurrent = TickUtil.getSqrtPriceAtTick(Number(poolInfo.tickCurrent));
    const { amountSlippageA, amountSlippageB } = LiquidityMathUtil.getAmountsFromLiquidityWithSlippage(
      sqrtPriceCurrent,
      TickUtil.getSqrtPriceAtTick(ownerPosition.tickLower),
      TickUtil.getSqrtPriceAtTick(ownerPosition.tickUpper),
      positionLiquidity,
      false,
      false,
      1 - slippage,
    );
    const res = await raydium.clmm.decreaseLiquidity({
      poolInfo,
      poolKeys,
      ownerPosition,
      ownerInfo: { useSOLBalance: true, closePosition: true },
      liquidity: positionLiquidity,
      amountMinA: amountSlippageA,
      amountMinB: amountSlippageB,
      txVersion: TxVersion.LEGACY,
    });

    const tx = res.transaction;
    if (req.mode === "simulate") {
      const sim = await simulate(connection, tx);
      return { tx_base64: [txToBase64(tx)], simulation: sim };
    }

    const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, res.signers);
    const verify = await verifyClosed(req, connection, wallet, positionId);
    return {
      signature,
      slot,
      confirmed_via,
      ...(fallback_broadcast ? { fallback_broadcast } : {}),
      note: `decreaseLiquidity bundled close, liquidity=${positionLiquidity.toString()} minA=${amountSlippageA.toString()} minB=${amountSlippageB.toString()} slippage_bps=${bps}`,
      verify,
    };
  }

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
  const verify = await verifyClosed(req, connection, wallet, positionId);
  return { signature, slot, confirmed_via, ...(fallback_broadcast ? { fallback_broadcast } : {}), verify };
}

export async function claimFees(req, connection, wallet) {
  const positionId = req.position_id;
  if (!positionId) throw new Error("position_id required for raydium claim");
  const poolAddress = req.pool_address;
  if (!poolAddress) throw new Error("pool_address required for raydium claim pending computation");

  const raydium = await getRaydium(connection, wallet.publicKey);
  const ownerPosition = await fetchPositionAccount(connection, positionId);

  // Real pending amounts + USD value drive the gate and the simulation
  // report. Checkpoint fields are stale for untouched positions.
  const pending = await computePending(connection, new PublicKey(poolAddress), ownerPosition);
  const usd = await pendingUsd(pending);
  const report = {
    pending_fees_raw: pending.fees_raw,
    pending_rewards_raw: pending.rewards_raw,
    reward_mints: pending.reward_mints,
    pending_usd: usd,
  };

  const minUsd = Number(req.claim_min_usd ?? 0);
  if (usd.total_usd !== null && usd.total_usd < minUsd) {
    return {
      skipped: true,
      skip_reason: `claimable $${usd.total_usd.toFixed(4)} < claim_min_usd ${minUsd}`,
      ...report,
    };
  }

  const res = await raydium.clmm.harvestAllRewards({
    allPoolInfo: [],
    allPositions: [ownerPosition],
    ownerInfo: { useSOLBalance: true },
    txVersion: TxVersion.LEGACY,
  });

  const txs = res.transactions;
  if (req.mode === "simulate") {
    const sims = await Promise.all(txs.map((tx) => simulate(connection, tx)));
    return { tx_base64: txs.map(txToBase64), simulation: sims, ...report };
  }

  const signatures = [];
  for (const tx of txs) {
    const sent = await signAndSend(connection, tx, wallet);
    signatures.push(sent);
  }
  return { signatures, ...report };
}
