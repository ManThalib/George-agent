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

// Action -> handler export. Dispatch used to be an if/else-if chain whose last
// branch was the swap; an action the module did not export fell through with
// `result === undefined`, and the response was still {ok: true}. A signal for
// an unimplemented action (e.g. orca add_liquidity) therefore looked to the
// executor like a successful transaction. Look the handler up and require it
// to exist.
const ACTION_HANDLERS = {
  open: "openPosition",
  close: "closePosition",
  claim: "claimFees",
  add_liquidity: "addLiquidity",
  remove_liquidity: "removeLiquidity",
  swap: "swap",
};

function validateRequest(req) {
  if (!req) throw new Error("missing request");
  const required = ["dex", "action", "mode", "rpc_url", "wallet_public_key"];
  if (req.action !== "swap") required.push("pool_address");
  const missing = required.filter((k) => req[k] === undefined || req[k] === "");
  if (missing.length) throw new Error(`missing request fields: ${missing.join(", ")}`);
  if (!DEX_HANDLERS[req.dex]) throw new Error(`unsupported dex: ${req.dex}`);
  if (!["open", "close", "claim", "swap", "add_liquidity", "remove_liquidity"].includes(req.action)) throw new Error(`unsupported action: ${req.action}`);
  if (!["simulate", "send"].includes(req.mode)) throw new Error(`mode must be simulate or send, got ${req.mode}`);
  const exportName = ACTION_HANDLERS[req.action];
  if (typeof DEX_HANDLERS[req.dex][exportName] !== "function") {
    throw new Error(
      `dex ${req.dex} does not implement action ${req.action} (no ${exportName} export); refusing to report success`
    );
  }
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
    const actionFn = handler[ACTION_HANDLERS[req.action]];
    const result = await actionFn(req, connection, wallet);
    if (!result || typeof result !== "object") {
      // A handler that returns nothing cannot be verified downstream (no
      // signature, no verify block); treat it as a failure, not a success.
      throw new Error(`dex ${req.dex} action ${req.action} returned no result object`);
    }

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
