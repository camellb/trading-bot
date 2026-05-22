// One-off CLI to sign a Delfi license blob using the local keypair.
//
// Usage:
//   node sign-license.mjs <email>
//
// Reads apps/web/.keys/license-private.pem and prints
// <base64url(payload)>.<base64url(signature)> on stdout.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pemPath = path.resolve(here, "..", ".keys", "license-private.pem");

const email = (process.argv[2] || "").trim().toLowerCase();
if (!email) {
  console.error("usage: node sign-license.mjs <email>");
  process.exit(2);
}

const pem = fs.readFileSync(pemPath, "utf8");
const key = crypto.createPrivateKey(pem);
if (key.asymmetricKeyType !== "ed25519") {
  console.error(`expected Ed25519 key, got ${key.asymmetricKeyType}`);
  process.exit(2);
}

const payload = {
  email,
  id: crypto.randomUUID(),
  issued_at: new Date().toISOString(),
  sku: "delfi-personal-v1",
  version: 1,
};

const keys = Object.keys(payload).sort();
const sorted = {};
for (const k of keys) sorted[k] = payload[k];
const json = JSON.stringify(sorted);

const b64url = (buf) =>
  Buffer.from(buf).toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");

const encodedPayload = b64url(Buffer.from(json, "utf8"));
const sig = crypto.sign(null, Buffer.from(encodedPayload, "utf8"), key);
const blob = `${encodedPayload}.${b64url(sig)}`;

console.log(blob);
