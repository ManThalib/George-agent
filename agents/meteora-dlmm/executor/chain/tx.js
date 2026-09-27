/**
 * Low-level Solana transaction helpers used by all DEX handlers.
 */

import { Connection, VersionedTransaction, Transaction, Keypair, PublicKey } from "@solana/web3.js";
import { log } from "./common.js";

/**
 * Normalise a transaction produced by an SDK into a base object we can sign,
 * simulate and serialise. Accepts either a @solana/web3.js Transaction or
 * VersionedTransaction.
 */
export function asTransactions(input) {
  if (Array.isArray(input)) return input.map(asTransactions).flat();
  if (input instanceof Transaction || input instanceof VersionedTransaction) return [input];
  if (input.transaction) return asTransactions(input.transaction);
  if (input.transactions) return asTransactions(input.transactions);
  if (input.tx) return asTransactions(input.tx); // Orca TransactionBuilder wrapper
  throw new Error("unknown transaction type returned by SDK");
}

/**
 * Extract non-wallet signers from SDK output. Raydium returns signers; Meteora
 * may need a position keypair passed separately. The handler always returns
 * `extraSigners` separately, so this is mostly for sanity.
 */
export function extraSignersFrom(input) {
  if (!input) return [];
  if (Array.isArray(input)) return input.map(extraSignersFrom).flat();
  if (input.signers && Array.isArray(input.signers)) return input.signers.filter(Boolean);
  return [];
}

/**
 * Convert a Transaction/VersionedTransaction to base64 (unsigned or signed).
 */
export function txToBase64(tx) {
  if (tx instanceof VersionedTransaction) return Buffer.from(tx.serialize()).toString("base64");
  if (tx instanceof Transaction) return tx.serialize({ requireAllSignatures: false }).toString("base64");
  throw new Error("cannot serialise non-transaction object to base64");
}

/**
 * Prepare a fresh blockhash for the given transactions (mutates in place).
 */
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

/**
 * Simulate a transaction via RPC without signing.
 * Returns { ok, err, logs, units_consumed }.
 */
export async function simulate(connection, tx) {
  // A recent blockhash must be present before the transaction can be
  // serialised, so set it on the transaction itself.
  await setBlockhash(connection, tx);

  // simulation must be done with an account fee payer; we do not need real sigs.
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

/**
 * Fallback broadcast for the send path.
 *
 * Helius was observed (2026-09-26, journal jarvis-swap-001/002, jarvis-open-001)
 * accepting sendRawTransaction but dropping the tx: simulation passed, network
 * was healthy, yet getSignatureStatuses showed nothing until blockhash expiry,
 * while the identical raw tx landed within seconds on the public endpoint.
 * The fallback is safe against double-land: the same signed raw tx has the
 * same signature everywhere, so the cluster dedupes it.
 *
 * Configured once per CLI invocation from dex.js (request fields
 * `fallback_rpc_urls` + `fallback_delay_seconds`); no keys live in files —
 * key-bearing paid endpoints come via the SOLANA_RPC_FALLBACK_URLS env var.
 */
const DEFAULT_FALLBACK_DELAY_MS = 15000;
let sendFallbacks = { connections: [], delayMs: DEFAULT_FALLBACK_DELAY_MS };

export function configureSendFallbacks({ connections = [], delayMs } = {}) {
  sendFallbacks = { connections, delayMs: delayMs ?? DEFAULT_FALLBACK_DELAY_MS };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Broadcast an already-signed raw tx to the fallback endpoints.
 * skipPreflight: the identical tx already passed preflight on the primary;
 * a fallback preflight can spuriously fail with "already processed" once the
 * tx lands, and would only delay the rescue.
 */
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

/**
 * Sign and send a transaction, then confirm it.
 * @param {Connection} connection  primary RPC (used for send + confirmation)
 * @param {Transaction|VersionedTransaction} tx
 * @param {Keypair} wallet
 * @param {Keypair[]} extraSigners
 * @param {object} [opts] { fallbackConnections, fallbackDelayMs } — overrides
 *   the module-level fallbacks set by configureSendFallbacks.
 * @returns {Promise<{signature: string, slot: number, confirmed_via: string, fallback_broadcast?: object[]}>}
 */
export async function signAndSend(connection, tx, wallet, extraSigners = [], opts = {}) {
  // Capture the blockhash actually embedded in the tx; confirmation must be
  // measured against this blockhash's validity window.
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
    // Primary send itself failed: if fallbacks exist, broadcast now instead of
    // losing the tx, then still try to confirm via primary reads.
    const fallbackConns = opts.fallbackConnections ?? sendFallbacks.connections;
    if (!fallbackConns.length) throw err;
    log(`primary sendRawTransaction threw; broadcasting to ${fallbackConns.length} fallback endpoint(s):`, err);
    const broadcast = await broadcastRawTo(fallbackConns, raw);
    const anyOk = broadcast.find((r) => r.ok);
    if (!anyOk) throw err; // no endpoint accepted the tx; surface the primary error
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

/**
 * Confirm a signed raw tx, broadcasting it to fallback endpoints if the
 * primary shows nothing after fallbackDelayMs. Confirmation is decided on the
 * primary (reads on Helius are reliable); fallback endpoints are only used to
 * push the tx into the cluster and, if primary reads die, to read status.
 */
async function confirmSignedTx(connection, raw, signature, { blockhash, lastValidBlockHeight }, {
  fallbackConnections = [],
  fallbackDelayMs = DEFAULT_FALLBACK_DELAY_MS,
  broadcastResults = null,
} = {}) {
  // `signature` is the one returned by the endpoint that accepted the send.
  // Do NOT derive it from the raw bytes: the wire format is
  // [sig-count][sig][message], so naive raw[0:64] extraction is off by one
  // (observed live 2026-09-26: derived 2KaQZ6… vs real i8d2Mg…).
  let fallbacksBroadcast = broadcastResults !== null;
  let results = broadcastResults ?? [];
  let watchdogStop = false;

  const confirmPromise = connection.confirmTransaction(
    { signature, blockhash, lastValidBlockHeight },
    "confirmed"
  );

  // Watchdog: after fallbackDelayMs with no confirmation on the primary,
  // broadcast the identical raw tx to fallback endpoints and keep polling.
  // Resolves via deferred when a status read (primary or fallback) shows the
  // tx confirmed — covers the case where the primary RPC read path also dies.
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
              break; // primary answered; no need to probe fallback reads this cycle
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
    confirmPromise.catch(() => {}); // drain the loser, never leave a rejected race dangling
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

/**
 * Build a wallet-like object that exposes only a public key. Used by SDKs that
 * need a wallet/owner during instruction construction in simulate mode.
 */
export function publicKeyWallet(publicKey) {
  return { publicKey: new PublicKey(publicKey) };
}
