// apps/web/lib/license.ts
//
// License signing for Delfi.
//
// Architecture: every paid Delfi install is unlocked by a small text blob
// that is generated server-side after a successful Stripe payment and
// emailed to the buyer. The desktop app embeds an Ed25519 PUBLIC key and
// verifies the blob locally on every launch. The PRIVATE key never leaves
// the Vercel function -- it only signs, never gets shipped to clients.
//
// Why Ed25519 + offline verification, not a license server: the desktop
// app must keep working without an internet round-trip on every launch.
// We don't want a "phone home or the bot stops" failure mode -- that
// breaks the local-first promise on the marketing site. A signed blob
// the client can verify on its own ticks both boxes.
//
// Blob format (URL-safe, single line):
//
//   <base64url(payload_json)>.<base64url(signature)>
//
// Payload (canonical JSON, sorted keys):
//
//   { "email": string,
//     "id": uuid,
//     "issued_at": iso8601 string,
//     "sku":   string,            // "delfi-personal-v1" today
//     "version": 1 }
//
// Signature: Ed25519 over the UTF-8 bytes of the base64url-encoded
// payload. We sign the encoded form (not the raw JSON) so the verifier
// doesn't have to re-canonicalise the JSON before checking.
//
// Key rotation: if we ever burn the private key we add a `kid` field
// to the payload and ship a new public key in the next desktop release.
// V1 deliberately omits `kid` -- there's only one key.

import crypto from "node:crypto";

// ---- key loading -------------------------------------------------------

/**
 * Returns the server's Ed25519 private signing key.
 *
 * The key is provided as PEM via the LICENSE_SIGNING_KEY env var. We
 * accept the PEM either inline (with literal newlines or escaped \n) or
 * base64-encoded -- both are common ways to paste it into Vercel's env
 * UI without it getting mangled.
 *
 * Throws if the env var is missing or malformed; the webhook fails
 * closed in that case (no license issued, no email sent).
 */
export function loadSigningKey(): crypto.KeyObject {
  const raw = process.env.LICENSE_SIGNING_KEY;
  if (!raw) {
    throw new Error(
      "LICENSE_SIGNING_KEY is not set. Generate a keypair with " +
        "`node scripts/generate-license-keypair.mjs` and set the " +
        "private PEM in Vercel env vars (Production + Preview).",
    );
  }
  let pem = raw;
  // Vercel sometimes drops the literal newlines on paste; allow both
  // "\\n"-escaped and base64-wrapped variants.
  if (pem.includes("\\n")) {
    pem = pem.replace(/\\n/g, "\n");
  }
  if (!pem.includes("BEGIN")) {
    // Likely base64-wrapped. Decode and try again.
    try {
      pem = Buffer.from(pem, "base64").toString("utf8");
    } catch {
      // fall through; the createPrivateKey call below will throw
    }
  }
  return crypto.createPrivateKey(pem);
}

// ---- blob format -------------------------------------------------------

export interface LicensePayload {
  /** Buyer email at time of purchase. */
  email: string;
  /** Stable license id; matches `licenses.id` in Postgres. */
  id: string;
  /** ISO 8601 timestamp at issue time. */
  issued_at: string;
  /** Product SKU. v1 ships exactly one: "delfi-personal-v1". */
  sku: string;
  /** Schema version. Bump if payload shape changes. */
  version: 1;
}

function b64urlEncode(buf: Buffer | string): string {
  const b = typeof buf === "string" ? Buffer.from(buf, "utf8") : buf;
  return b
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function b64urlDecode(s: string): Buffer {
  // Re-pad to a multiple of 4 before decoding.
  const pad = s.length % 4 === 0 ? 0 : 4 - (s.length % 4);
  const fixed = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat(pad);
  return Buffer.from(fixed, "base64");
}

/** Canonical JSON: keys sorted, no spaces. Exact byte sequence the
 *  desktop verifier expects (we sign the encoded form, but humans
 *  reading the payload should see something stable). */
function canonicalJson(payload: LicensePayload): string {
  const keys = Object.keys(payload).sort() as (keyof LicensePayload)[];
  const obj: Record<string, unknown> = {};
  for (const k of keys) obj[k] = payload[k];
  return JSON.stringify(obj);
}

/**
 * Sign a license payload and return the `<payload>.<sig>` blob to
 * deliver to the buyer.
 */
export function signLicense(
  payload: LicensePayload,
  key: crypto.KeyObject = loadSigningKey(),
): string {
  if (key.asymmetricKeyType !== "ed25519") {
    throw new Error(
      `LICENSE_SIGNING_KEY must be Ed25519, got ${key.asymmetricKeyType}`,
    );
  }
  const json = canonicalJson(payload);
  const encodedPayload = b64urlEncode(json);
  // Ed25519 in node: pass null as the digest algorithm.
  const sig = crypto.sign(null, Buffer.from(encodedPayload, "utf8"), key);
  return `${encodedPayload}.${b64urlEncode(sig)}`;
}

/**
 * Verify a license blob with a public key. Returns the decoded payload
 * on success; throws on malformed input or bad signature.
 *
 * Used by tests and the issue route's self-test; the production
 * verifier lives in the desktop app (Python, in
 * Delfibot/bot/engine/license.py) and uses an embedded copy of the
 * public key, not this function.
 */
export function verifyLicense(
  blob: string,
  publicKey: crypto.KeyObject,
): LicensePayload {
  const parts = blob.split(".");
  if (parts.length !== 2) throw new Error("license: malformed blob");
  const [encodedPayload, encodedSig] = parts;
  const sig = b64urlDecode(encodedSig);
  const ok = crypto.verify(
    null,
    Buffer.from(encodedPayload, "utf8"),
    publicKey,
    sig,
  );
  if (!ok) throw new Error("license: bad signature");
  return JSON.parse(b64urlDecode(encodedPayload).toString("utf8"));
}

// ---- payload builder ---------------------------------------------------

export const DELFI_SKU_PERSONAL_V1 = "delfi-personal-v1";

/**
 * Build a fresh license payload. Random UUID; current timestamp; SKU
 * defaults to the only one we sell today.
 */
export function buildPayload(args: {
  email: string;
  sku?: string;
  id?: string;
  issuedAt?: Date;
}): LicensePayload {
  return {
    email: args.email.trim().toLowerCase(),
    id: args.id ?? crypto.randomUUID(),
    issued_at: (args.issuedAt ?? new Date()).toISOString(),
    sku: args.sku ?? DELFI_SKU_PERSONAL_V1,
    version: 1,
  };
}
