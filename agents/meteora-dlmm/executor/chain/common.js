/**
 * Shared helpers for the multi-DEX chain CLI.
 *
 * Protocol: one JSON request object on stdin, one JSON response object on stdout.
 * ALL diagnostics go to stderr so stdout stays parseable.
 */

import process from "node:process";
import { Keypair } from "@solana/web3.js";
import bs58 from "bs58";

const stderrLog = (...args) => process.stderr.write(args.map(String).join(" ") + "\n");
for (const fn of ["log", "info", "debug", "warn", "trace"]) {
  console[fn] = stderrLog;
}

export function respond(obj, onFlush) {
  process.stdout.write(JSON.stringify(obj) + "\n", onFlush);
}

export function respondError(stage, err) {
  respond({
    ok: false,
    stage,
    error: err instanceof Error ? err.message : String(err),
    error_name: err instanceof Error ? err.name : undefined,
  });
}

export function log(...args) {
  stderrLog("[chain]", ...args);
}

export async function readRequest() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) throw new Error("empty request on stdin");
  return JSON.parse(raw);
}

/**
 * Load the signing keypair for a logical wallet id.
 *
 * Multi-wallet mirror: MAIN uses SOLANA_AGENT_WALLET (existing behavior);
 * any other wallet_id resolves SOLANA_WALLET_<ID-UPPERCASED> the same way.
 * The dispatcher sets the env var for the leg before spawning this process.
 * The public key must still match the request's wallet_public_key (checked
 * by dispatch.js) — a registry misroute fails closed.
 */
export function loadKeypairForWallet(walletId) {
  const id = (walletId || "main").trim().toLowerCase();
  if (id === "main" || !id) return loadKeypair();
  const envName = `SOLANA_WALLET_${id.replace(/[^A-Z0-9]/gi, "_").toUpperCase()}`;
  const prev = process.env.SOLANA_AGENT_WALLET;
  if (!process.env[envName]) {
    throw new Error(`${envName} not set: no keypair registered for wallet '${id}'`);
  }
  process.env.SOLANA_AGENT_WALLET = process.env[envName];
  try {
    return loadKeypair();
  } finally {
    process.env.SOLANA_AGENT_WALLET = prev;
  }
}

export function loadKeypair() {
  const raw = process.env.SOLANA_AGENT_WALLET?.trim();
  if (!raw) throw new Error("SOLANA_AGENT_WALLET not set");

  let bytes;
  if (raw.startsWith("[") && raw.endsWith("]")) {
    bytes = new Uint8Array(JSON.parse(raw));
  } else {
    try {
      bytes = bs58.decode(raw);
    } catch (err) {
      if (raw.startsWith("oc-")) {
        throw new Error(`SOLANA_AGENT_WALLET is an OpenClaw secret sentinel; re-connect the secret in Agent Settings (${err.message})`);
      }
      throw new Error(`SOLANA_AGENT_WALLET is not a valid base58 keypair: ${err.message}`);
    }
  }

  if (bytes.length === 64) {
    return Keypair.fromSecretKey(bytes);
  } else if (bytes.length === 32) {
    return Keypair.fromSeed(bytes);
  }
  throw new Error(`keypair material unexpected length ${bytes.length}; expected 32 or 64 bytes`);
}
