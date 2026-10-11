import { createRequire } from "module";
import { Connection, PublicKey } from "@solana/web3.js";
const require = createRequire(import.meta.url);
const DLMM = require("@meteora-ag/dlmm");
const RPC = process.env.SOLANA_RPC_URL;
const c = new Connection(RPC, "confirmed");
for (const addr of process.argv.slice(2)) {
  const dlmm = await DLMM.create(c, new PublicKey(addr));
  const ab = await dlmm.getActiveBin();
  console.log(addr, "activeBinId:", Number(ab?.binId), "price:", String(ab?.price));
}
