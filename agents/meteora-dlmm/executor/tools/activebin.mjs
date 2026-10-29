/**
 * Pool inspector: print the active bin id and price for each pool address.
 *
 * Usage (needs SOLANA_RPC_URL and executor/node_modules installed):
 *   node tools/activebin.mjs <pool_address> [more_pool_addresses...]
 *
 * Read-only. This was previously a stray `activebin.mjs` at the agent root
 * with no documentation; it is kept because it is genuinely useful when
 * triaging a range that has drifted out of the active bin.
 */

import { createRequire } from "module";
import { Connection, PublicKey } from "@solana/web3.js";
const require = createRequire(import.meta.url);
const DLMM = require("@meteora-ag/dlmm");
const RPC = process.env.SOLANA_RPC_URL;

if (!RPC) {
  console.error("SOLANA_RPC_URL is not set");
  process.exit(1);
}
if (process.argv.length < 3) {
  console.error("usage: node tools/activebin.mjs <pool_address> [...]");
  process.exit(2);
}

const c = new Connection(RPC, "confirmed");
for (const addr of process.argv.slice(2)) {
  const dlmm = await DLMM.create(c, new PublicKey(addr));
  const ab = await dlmm.getActiveBin();
  console.log(addr, "activeBinId:", Number(ab?.binId), "price:", String(ab?.price));
}
