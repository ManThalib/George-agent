#!/usr/bin/env node
/**
 * dex.js — chain CLI entry point.
 *
 * Subcommands (determined by request.action and request.mode):
 *   open, close, claim
 *
 * Reads a JSON request from stdin, dispatches to the correct DEX handler,
 * and emits a JSON response on stdout. All diagnostics go to stderr.
 *
 * Environment:
 *   SOLANA_AGENT_WALLET  -> base58 64-byte keypair (or 32-byte seed) used in send mode.
 */

import process from "node:process";
import { Connection, PublicKey, Keypair } from "@solana/web3.js";
import { respond, respondError, readRequest, log } from "./common.js";
import * as meteora from "./meteora.js";
import * as raydium from "./raydium.js";
import * as orca from "./orca.js";
import * as jupiter from "./jupiter.js";
import bs58 from "bs58";

const DEX_HANDLERS = { meteora, raydium, orca, jupiter };

function loadKeypair() {
  const raw = process.env.SOLANA_AGENT_WALLET?.trim();
  if (!raw) throw new Error("SOLANA_AGENT_WALLET not set");

  let bytes;
  // JSON array of numbers
  if (raw.startsWith("[") && raw.endsWith("]")) {
    bytes = new Uint8Array(JSON.parse(raw));
  } else {
    // base58 encoded
    bytes = bs58.decode(raw);
  }

  if (bytes.length === 64) {
    return Keypair.fromSecretKey(bytes);
  } else if (bytes.length === 32) {
    return Keypair.fromSeed(bytes);
  }
  throw new Error(`keypair material unexpected length ${bytes.length}; expected 32 or 64 bytes`);
}

function validateRequest(req) {
  if (!req) throw new Error("missing request");
  const required = ["dex", "action", "mode", "rpc_url", "wallet_public_key"];
  if (req.action !== "swap") required.push("pool_address");
  const missing = required.filter((k) => req[k] === undefined || req[k] === "");
  if (missing.length) throw new Error(`missing request fields: ${missing.join(", ")}`);
  if (!DEX_HANDLERS[req.dex]) throw new Error(`unsupported dex: ${req.dex}`);
  if (!["open", "close", "claim", "swap"].includes(req.action)) throw new Error(`unsupported action: ${req.action}`);
  if (!["simulate", "send"].includes(req.mode)) throw new Error(`mode must be simulate or send, got ${req.mode}`);
}

async function main() {
  const req = await readRequest().catch((err) => {
    respondError("read_request", err);
    process.exit(1);
  });

  try {
    validateRequest(req);

    const connection = new Connection(req.rpc_url, {
      commitment: req.commitment || "confirmed",
      wsEndpoint: req.rpc_ws_url || undefined,
    });

    let wallet;
    if (req.mode === "send") {
      wallet = loadKeypair();
      const publicKey = new PublicKey(req.wallet_public_key);
      if (!publicKey.equals(wallet.publicKey)) {
        throw new Error(`keypair public key ${wallet.publicKey.toBase58()} does not match configured wallet ${req.wallet_public_key}`);
      }
    } else {
      wallet = { publicKey: new PublicKey(req.wallet_public_key) };
    }

    const handler = DEX_HANDLERS[req.dex];
    let result;
    if (req.action === "open") result = await handler.openPosition(req, connection, wallet);
    else if (req.action === "close") result = await handler.closePosition(req, connection, wallet);
    else if (req.action === "claim") result = await handler.claimFees(req, connection, wallet);
    else if (req.action === "swap") result = await jupiter.swap(req, connection, wallet);

    respond({ ok: true, dex: req.dex, action: req.action, mode: req.mode, ...result });
  } catch (err) {
    log("uncaught error:", err);
    respondError("dispatch", err);
    process.exit(1);
  }
}

main();
