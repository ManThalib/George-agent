#!/usr/bin/env node
/**
 * Chain dispatch entry point.
 *
 * Reads a JSON request from stdin, dispatches to the correct DEX handler in
 * dexes/<dex>/chain.js, and emits a JSON response on stdout.
 */

import process from "node:process";
import { Connection, PublicKey } from "@solana/web3.js";
import { respond, respondError, readRequest, log, loadKeypair, loadKeypairForWallet } from "./common.js";
import { configureSendFallbacks } from "./tx.js";
import * as meteora from "../dexes/meteora/chain.js";
import * as raydium from "../dexes/raydium/chain.js";
import * as orca from "../dexes/orca/chain.js";
import * as jupiter from "./jupiter.js";

const DEX_HANDLERS = { meteora, raydium, orca, jupiter };

function validateRequest(req) {
  if (!req) throw new Error("missing request");
  const required = ["dex", "action", "mode", "rpc_url", "wallet_public_key"];
  if (req.action !== "swap") required.push("pool_address");
  const missing = required.filter((k) => req[k] === undefined || req[k] === "");
  if (missing.length) throw new Error(`missing request fields: ${missing.join(", ")}`);
  if (!DEX_HANDLERS[req.dex]) throw new Error(`unsupported dex: ${req.dex}`);
  if (!["open", "close", "claim", "swap", "add_liquidity", "remove_liquidity"].includes(req.action)) throw new Error(`unsupported action: ${req.action}`);
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

    const fallbackUrls = Array.isArray(req.fallback_rpc_urls) ? req.fallback_rpc_urls : [];
    const badFallbacks = fallbackUrls.filter((u) => typeof u !== "string" || !u.startsWith("https://"));
    if (badFallbacks.length) throw new Error(`fallback_rpc_urls must be https URLs, got: ${badFallbacks.join(", ")}`);
    configureSendFallbacks({
      connections: fallbackUrls.map((u) => new Connection(u, { commitment: req.commitment || "confirmed" })),
      delayMs: Number(req.fallback_delay_seconds) > 0 ? Number(req.fallback_delay_seconds) * 1000 : undefined,
    });
    if (fallbackUrls.length) log(`send fallbacks armed: ${fallbackUrls.length} endpoint(s), delay ${Number(req.fallback_delay_seconds) || 15}s`);

    let wallet;
    if (req.mode === "send") {
      wallet = loadKeypairForWallet(req.wallet_id);
      const publicKey = new PublicKey(req.wallet_public_key);
      if (!publicKey.equals(wallet.publicKey)) {
        throw new Error(`keypair public key ${wallet.publicKey.toBase58()} does not match configured wallet ${req.wallet_public_key} (wallet_id=${req.wallet_id || "main"})`);
      }
    } else {
      wallet = { publicKey: new PublicKey(req.wallet_public_key) };
    }

    const handler = DEX_HANDLERS[req.dex];
    let result;
    if (req.action === "open") result = await handler.openPosition(req, connection, wallet);
    else if (req.action === "close") result = await handler.closePosition(req, connection, wallet);
    else if (req.action === "claim") result = await handler.claimFees(req, connection, wallet);
    else if (req.action === "add_liquidity") result = await handler.addLiquidity(req, connection, wallet);
    else if (req.action === "remove_liquidity") result = await handler.removeLiquidity(req, connection, wallet);
    else if (req.action === "swap") result = await jupiter.swap(req, connection, wallet);

    respond({ ok: true, dex: req.dex, action: req.action, mode: req.mode, ...result }, () => {
      if (req.mode === "send") process.exit(0);
    });
  } catch (err) {
    log("uncaught error:", err);
    respondError("dispatch", err);
    process.exit(1);
  }
}

main();
