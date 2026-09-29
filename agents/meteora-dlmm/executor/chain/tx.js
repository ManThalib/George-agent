/**
 * Low-level Solana transaction helpers used by all DEX handlers.
 */

import { Connection, VersionedTransaction, Transaction, PublicKey } from "@solana/web3.js";
import { log } from "./common.js";

export function asTransactions(input) {
  if (Array.isArray(input)) return input.map(asTransactions).flat();
  if (input instanceof Transaction || input instanceof VersionedTransaction) return [input];
  if (input.transaction) return asTransactions(input.transaction);
  if (input.transactions) return asTransactions(input.transactions);
  if (input.tx) return asTransactions(input.tx);
  throw new Error("unknown transaction type returned by SDK");
}

export function extraSignersFrom(input) {
  if (!input) return [];
  if (Array.isArray(input)) return input.map(extraSignersFrom).flat();
  if (input.signers && Array.isArray(input.signers)) return input.signers.filter(Boolean);
  return [];
}

export function txToBase64(tx) {
  if (tx instanceof VersionedTransaction) return Buffer.from(tx.serialize()).toString("base64");
  if (tx instanceof Transaction) return tx.serialize({ requireAllSignatures: false }).toString("base64");
  throw new Error("cannot serialise non-transaction object to base64");
}

export async function setBlockhash(connection, txOrTxs) {
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash("confirmed");
  for (const tx of asTransactions(txOrTxs).flat()) {
    if (tx instanceof VersionedTransaction) {
      tx.message.recentBlockhash = blockhash;
    } else {
      tx.recentBlockhash = blockhash;
    }
  }
  return { blockhash, lastValidBlockHeight };
}

export async function simulate(connection, tx) {
  await setBlockhash(connection, tx);
  const sim = tx instanceof VersionedTransaction
    ? await connection.simulateTransaction(tx, { commitment: "confirmed" })
    : await connection.simulateTransaction(tx);
  return {
    ok: sim.value.err === null,
    err: sim.value.err ?? null,
    logs: sim.value.logs ?? [],
    units_consumed: sim.value.unitsConsumed ?? null,
  };
}

const DEFAULT_FALLBACK_DELAY_MS = 15000;
let sendFallbacks = { connections: [], delayMs: DEFAULT_FALLBACK_DELAY_MS };

export function configureSendFallbacks({ connections = [], delayMs } = {}) {
  sendFallbacks = { connections, delayMs: delayMs ?? DEFAULT_FALLBACK_DELAY_MS };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function broadcastRawTo(connections, raw) {
  const results = [];
  for (const conn of connections) {
    try {
      const sig = await conn.sendRawTransaction(raw, { skipPreflight: true, maxRetries: 5 });
      results.push({ endpoint: conn.rpcEndpoint, ok: true, signature: sig });
      log(`fallback broadcast ok via ${conn.rpcEndpoint}: ${sig}`);
    } catch (err) {
      results.push({ endpoint: conn.rpcEndpoint, ok: false, error: String(err?.message ?? err) });
      log(`fallback broadcast failed via ${conn.rpcEndpoint}:`, err);
    }
  }
  return results;
}

export async function signAndSend(connection, tx, wallet, extraSigners = [], opts = {}) {
  const { blockhash, lastValidBlockHeight } = await setBlockhash(connection, tx);

  if (tx instanceof VersionedTransaction) {
    tx.sign([wallet, ...extraSigners]);
  } else {
    tx.sign(wallet, ...extraSigners);
  }

  const raw = tx.serialize();
  let signature;
  try {
    signature = await connection.sendRawTransaction(raw, {
      skipPreflight: false,
      preflightCommitment: "confirmed",
      maxRetries: 3,
    });
  } catch (err) {
    const fallbackConns = opts.fallbackConnections ?? sendFallbacks.connections;
    if (!fallbackConns.length) throw err;
    log(`primary sendRawTransaction threw; broadcasting to ${fallbackConns.length} fallback endpoint(s):`, err);
    const broadcast = await broadcastRawTo(fallbackConns, raw);
    const anyOk = broadcast.find((r) => r.ok);
    if (!anyOk) throw err;
    return await confirmSignedTx(connection, raw, anyOk.signature, { blockhash, lastValidBlockHeight }, {
      fallbackConnections: fallbackConns,
      broadcastResults: broadcast,
    });
  }

  log("sent tx", signature, "awaiting confirmation...");
  return await confirmSignedTx(connection, raw, signature, { blockhash, lastValidBlockHeight }, {
    fallbackConnections: opts.fallbackConnections ?? sendFallbacks.connections,
    fallbackDelayMs: opts.fallbackDelayMs ?? sendFallbacks.delayMs,
  });
}

async function confirmSignedTx(connection, raw, signature, { blockhash, lastValidBlockHeight }, {
  fallbackConnections = [],
  fallbackDelayMs = DEFAULT_FALLBACK_DELAY_MS,
  broadcastResults = null,
} = {}) {
  let fallbacksBroadcast = broadcastResults !== null;
  let results = broadcastResults ?? [];
  let watchdogStop = false;

  const confirmPromise = connection.confirmTransaction(
    { signature, blockhash, lastValidBlockHeight },
    "confirmed"
  );

  let resolveWatchdog;
  const watchdogPromise = new Promise((resolve) => { resolveWatchdog = resolve; });
  const startedAt = Date.now();
  (async () => {
    while (!watchdogStop) {
      try {
        if (!fallbacksBroadcast && fallbackConnections.length &&
            Date.now() - startedAt >= fallbackDelayMs) {
          log(`no confirmation after ${fallbackDelayMs}ms via primary; broadcasting identical raw tx to ${fallbackConnections.length} fallback endpoint(s)`);
          results = await broadcastRawTo(fallbackConnections, raw);
          fallbacksBroadcast = true;
        }
        if (fallbacksBroadcast) {
          for (const conn of [connection, ...fallbackConnections]) {
            try {
              const statuses = await conn.getSignatureStatuses([signature], { searchTransactionHistory: false });
              const st = statuses?.value?.[0];
              if (st && ["confirmed", "finalized"].includes(st.confirmationStatus)) {
                resolveWatchdog({ slot: st.slot ?? null });
                return;
              }
              break;
            } catch { /* read failed; try next endpoint */ }
          }
        }
      } catch (err) {
        log("fallback watchdog error (continuing):", err);
      }
      await sleep(2000);
    }
  })();

  try {
    const winner = await Promise.race([
      confirmPromise.then((r) => ({ src: "primary-confirm", ctx: r })),
      watchdogPromise.then((r) => ({ src: "fallback-read", ctx: r })),
    ]);
    watchdogStop = true;
    confirmPromise.catch(() => {});
    const slot = winner.ctx?.context?.slot ?? winner.ctx?.slot ?? lastValidBlockHeight;
    const out = { signature, slot, confirmed_via: winner.src };
    if (results.length) out.fallback_broadcast = results;
    return out;
  } catch (err) {
    watchdogStop = true;
    const fbNote = fallbackConnections.length
      ? fallbacksBroadcast
        ? ` (fallback endpoints were broadcast: ${results.map((r) => `${r.endpoint}:${r.ok ? "ok" : "failed"}`).join(", ")})`
        : ` (fallbacks configured: ${fallbackConnections.length}, broadcast delay ${fallbackDelayMs}ms not reached)`
      : " (no fallback endpoints configured)";
    if (String(err?.message ?? err).includes("block height exceeded")) {
      throw new Error(`transaction ${signature} expired before confirmation${fbNote}: ${err.message}`);
    }
    throw err;
  }
}
