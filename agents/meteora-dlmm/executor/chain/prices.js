/**
 * USD pricing via Jupiter lite price API (used by claim gating).
 */

const PRICE_BASE = "https://lite-api.jup.ag/price/v3";

const priceCache = new Map(); // mint -> { price, at }
const CACHE_TTL_MS = 60_000;

async function fetchJson(url) {
  const res = await fetch(url);
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`Jupiter price request failed (${res.status}) at ${url.split("?")[0]}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error("Jupiter price returned non-JSON response");
  }
}

/**
 * Fetch USD prices for mints. Returns {mint: usdPrice}; mints Jupiter cannot
 * price are absent. Cached for 60s per process (one claim run is short-lived).
 */
export async function fetchUsdPrices(mints) {
  const wanted = [...new Set(mints.filter(Boolean))];
  const out = {};
  const stale = [];
  const now = Date.now();
  for (const mint of wanted) {
    const hit = priceCache.get(mint);
    if (hit && now - hit.at < CACHE_TTL_MS) out[mint] = hit.price;
    else stale.push(mint);
  }
  if (stale.length) {
    const data = await fetchJson(`${PRICE_BASE}?ids=${stale.join(",")}`);
    for (const mint of stale) {
      const entry = data?.[mint];
      const price = Number(entry?.usdPrice);
      if (Number.isFinite(price) && price > 0) {
        priceCache.set(mint, { price, at: now });
        out[mint] = price;
      }
    }
  }
  return out;
}

// SOL mint constant for gas-cost pricing.
export const SOL_MINT = "So11111111111111111111111111111111111111112";
