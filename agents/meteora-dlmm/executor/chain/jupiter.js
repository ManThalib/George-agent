/**
 * Jupiter swap handler.
 */

import { VersionedTransaction } from "@solana/web3.js";
import { simulate, signAndSend, txToBase64 } from "./tx.js";

const JUPITER_BASE = "https://lite-api.jup.ag/swap/v1";
const MAX_PRIORITY_LAMPORTS = 500000;

function assertMint(value, field) {
  if (!value || typeof value !== "string" || value.length < 32) {
    throw new Error(`${field} must be a base58 mint address`);
  }
}

async function fetchJson(url, init) {
  const res = await fetch(url, init);
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`Jupiter request failed (${res.status}) at ${url.split("?")[0]}: ${text.slice(0, 300)}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`Jupiter returned non-JSON response: ${text.slice(0, 300)}`);
  }
}

export async function swap(req, connection, wallet) {
  const inputMint = req.input_mint;
  const outputMint = req.output_mint;
  assertMint(inputMint, "input_mint");
  assertMint(outputMint, "output_mint");

  const amount = String(req.amount ?? "").trim();
  if (!/^[0-9]+$/.test(amount) || amount === "0") {
    throw new Error(`amount must be a positive integer string of base units, got: ${req.amount}`);
  }

  const slippageBps = Number(req.max_slippage_bps ?? 100);
  const swapMode = req.exact_out === true ? "ExactOut" : "ExactIn";

  const quoteParams = new URLSearchParams({
    inputMint,
    outputMint,
    amount,
    slippageBps: String(slippageBps),
    swapMode,
    restrictIntermediateTokens: "true",
  });

  const quote = await fetchJson(`${JUPITER_BASE}/quote?${quoteParams.toString()}`);
  const priceImpactPct = Math.abs(Number(quote.priceImpactPct ?? 0)) * 100;
  const maxImpactPct = Number(req.max_price_impact_pct ?? 1.5);
  if (priceImpactPct > maxImpactPct) {
    throw new Error(`price impact ${priceImpactPct.toFixed(4)}% exceeds rail max_price_impact_pct ${maxImpactPct}%`);
  }

  const swapBody = {
    quoteResponse: quote,
    userPublicKey: wallet.publicKey.toBase58(),
    wrapAndUnwrapSol: true,
    dynamicComputeUnitLimit: true,
    prioritizationFeeLamports: {
      priorityLevelWithMaxLamports: {
        maxLamports: MAX_PRIORITY_LAMPORTS,
        priorityLevel: "high",
      },
    },
  };

  const swapJson = await fetchJson(`${JUPITER_BASE}/swap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(swapBody),
  });

  if (!swapJson.swapTransaction) {
    throw new Error(`Jupiter returned no swapTransaction: ${JSON.stringify(swapJson).slice(0, 300)}`);
  }

  const tx = VersionedTransaction.deserialize(Buffer.from(swapJson.swapTransaction, "base64"));

  const routeLabels = (quote.routePlan ?? [])
    .map((r) => r.swapInfo?.label)
    .filter(Boolean)
    .join("+");
  const notes = [
    `jupiter ${swapMode}`,
    `in=${inputMint} out=${outputMint}`,
    `inAmount=${quote.inAmount} outAmount=${quote.outAmount}`,
    `minOut=${quote.otherAmountThreshold}`,
    `impact=${priceImpactPct.toFixed(4)}%`,
    `route=${routeLabels || "n/a"}`,
  ].join(" ");

  if (req.mode === "simulate") {
    const sim = await simulate(connection, tx);
    return { tx_base64: [txToBase64(tx)], simulation: sim, notes };
  }

  const { signature, slot, confirmed_via, fallback_broadcast } = await signAndSend(connection, tx, wallet, []);
  return {
    signature,
    slot,
    confirmed_via,
    ...(fallback_broadcast ? { fallback_broadcast } : {}),
    notes,
    quote: {
      input_mint: inputMint,
      output_mint: outputMint,
      in_amount: quote.inAmount,
      out_amount: quote.outAmount,
      min_out_amount: quote.otherAmountThreshold,
      price_impact_pct: priceImpactPct,
      route: routeLabels,
    },
  };
}
