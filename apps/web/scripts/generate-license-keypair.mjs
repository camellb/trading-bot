// apps/web/scripts/generate-license-keypair.mjs
//
// Bootstrap script: generate a fresh Ed25519 keypair for license signing.
//
// Usage (one-off, from apps/web/):
//
//   node scripts/generate-license-keypair.mjs
//
// Output:
//
//   .keys/license-private.pem   <-- goes into LICENSE_SIGNING_KEY in Vercel
//   .keys/license-public.pem    <-- goes into the desktop app
//   .keys/license-public.b64    <-- same key, base64 (handy for embedding)
//
// .keys/ is gitignored (see apps/web/.gitignore). Never commit either
// half of this keypair to git: the private key signs new licenses, the
// public key is what the desktop app embeds for offline verification.
// If the private key leaks, every license ever issued is forgeable
// until you rotate.
//
// Rotation: generate a new pair, ship a new desktop release embedding
// the new public key, and add a `kid` field to license payloads so old
// installs can still verify old licenses while new ones use the new
// pair. V1 deliberately doesn't have `kid` -- if we have to rotate
// before v1.1, we cut a desktop release that accepts both old and new
// public keys for a grace window.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.resolve(__dirname, "..", ".keys");
fs.mkdirSync(outDir, { recursive: true });

const { privateKey, publicKey } = crypto.generateKeyPairSync("ed25519");

const privPem = privateKey.export({ format: "pem", type: "pkcs8" });
const pubPem = publicKey.export({ format: "pem", type: "spki" });
// Raw 32-byte public key, base64-encoded -- this is the form the
// Python verifier in Delfibot/bot/engine/license.py is set up to load
// directly with `nacl.signing.VerifyKey(b64decode(...))`.
const pubRaw = publicKey.export({ format: "der", type: "spki" });
// SubjectPublicKeyInfo: 12-byte ASN.1 prefix + 32-byte raw key.
const pubB64 = Buffer.from(pubRaw.subarray(pubRaw.length - 32)).toString(
  "base64",
);

fs.writeFileSync(path.join(outDir, "license-private.pem"), privPem, {
  mode: 0o600,
});
fs.writeFileSync(path.join(outDir, "license-public.pem"), pubPem);
fs.writeFileSync(path.join(outDir, "license-public.b64"), pubB64 + "\n");

console.log("Wrote:");
console.log(`  ${path.join(outDir, "license-private.pem")} (chmod 600)`);
console.log(`  ${path.join(outDir, "license-public.pem")}`);
console.log(`  ${path.join(outDir, "license-public.b64")}`);
console.log("");
console.log("Next steps:");
console.log("  1. Set LICENSE_SIGNING_KEY in Vercel to the contents of");
console.log("     license-private.pem (Production + Preview).");
console.log("  2. Embed license-public.b64 (the 32-byte raw key) in the");
console.log("     desktop verifier at Delfibot/bot/engine/license.py.");
console.log("  3. Do not commit .keys/ to git.");
