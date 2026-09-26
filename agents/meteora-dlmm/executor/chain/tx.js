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
 * Sign and send a transaction, then confirm it.
 * @param {Connection} connection
 * @param {Transaction|VersionedTransaction} tx
 * @param {Keypair[]} extraSigners
 * @returns {Promise<{signature: string, slot: number}>}
 */
export async function signAndSend(connection, tx, wallet, extraSigners = []) {
  await setBlockhash(connection, tx);

  if (tx instanceof VersionedTransaction) {
    tx.sign([wallet, ...extraSigners]);
  } else {
    tx.sign(wallet, ...extraSigners);
  }

  const raw = tx.serialize();
  const signature = await connection.sendRawTransaction(raw, {
    skipPreflight: false,
    preflightCommitment: "confirmed",
    maxRetries: 3,
  });

  log("sent tx", signature, "awaiting confirmation...");
  const latest = await connection.getLatestBlockhash("confirmed");
  await connection.confirmTransaction(
    { signature, blockhash: latest.blockhash, lastValidBlockHeight: latest.lastValidBlockHeight },
    "confirmed"
  );
  return { signature, slot: latest.lastValidBlockHeight };
}

/**
 * Build a wallet-like object that exposes only a public key. Used by SDKs that
 * need a wallet/owner during instruction construction in simulate mode.
 */
export function publicKeyWallet(publicKey) {
  return { publicKey: new PublicKey(publicKey) };
}
