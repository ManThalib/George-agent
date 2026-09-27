/**
 * Shared helpers for the multi-DEX chain CLI.
 *
 * Protocol: one JSON request object on stdin, one JSON response object on stdout.
 * ALL diagnostics go to stderr so stdout stays parseable.
 *
 * The private key is read from the SOLANA_AGENT_WALLET environment variable and
 * is never logged, echoed, or included in any response.
 */

import process from "node:process";

/* ------------------------------------------------------------------ *
 * Output discipline
 * ------------------------------------------------------------------ */

// SDKs occasionally console.log(); that would corrupt our JSON on stdout.
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

/* ------------------------------------------------------------------ *
 * stdin
 * ------------------------------------------------------------------ */

export async function readRequest() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) throw new Error("empty request on stdin");
  return JSON.parse(raw);
}

/* ------------------------------------------------------------------ *
 * CLI flags
 * ------------------------------------------------------------------ */

export function parseFlags(argv) {
  const flags = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        flags[key] = next;
        i++;
      } else {
        flags[key] = true;
      }
    } else {
      flags._.push(arg);
    }
  }
  return flags;
}

/* ------------------------------------------------------------------ *
 * Amount helpers
 * ------------------------------------------------------------------ */

/** Accept a raw integer string, a number, or a decimal UI amount + decimals. */
export function toRawAmount(value, decimals) {
  if (value === undefined || value === null || value === "") return 0n;
  if (typeof value === "string" && /^[0-9]+$/.test(value)) return BigInt(value);
  if (typeof value === "number" && Number.isInteger(value)) return BigInt(value);
  // Decimal UI amount -> raw, truncated (never rounds up: we never overspend).
  const s = String(value);
  if (!/^-?\d+(\.\d+)?$/.test(s)) throw new Error(`invalid amount: ${s}`);
  const neg = s.startsWith("-");
  const [intPart, fracPart = ""] = s.replace("-", "").split(".");
  const padded = (fracPart + "0".repeat(decimals)).slice(0, decimals);
  const raw = BigInt(intPart + padded);
  return neg ? -raw : raw;
}

/* ------------------------------------------------------------------ *
 * Response shape
 * ------------------------------------------------------------------ */

export function okResponse(base, extra = {}) {
  return { ok: true, ...base, ...extra };
}
