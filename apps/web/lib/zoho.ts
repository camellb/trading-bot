// apps/web/lib/zoho.ts
//
// Minimal Zoho Books client for the Stripe → Books pipeline.
//
// Why this exists:
//   * Each successful Stripe checkout becomes an Invoice + recorded
//     Customer Payment in Zoho Books, so the books reflect the sale
//     automatically (P&L, customer ledger, bank reconciliation).
//   * Each Stripe refund becomes a Credit Note against the original
//     invoice, so revenue is reversed correctly.
//
// Why a custom client and not an npm package: the surface area we
// need is four endpoints. Adding `zoho-books-sdk` would pull in
// significantly more code than this file plus a transitive auth
// library that handles OAuth differently than our token cache.
//
// Threading model: this module runs inside the Stripe webhook
// handler (Node runtime on Vercel), one event at a time. The
// access_token cache lives in module scope; it's safe because each
// Vercel function instance is single-process. A cold start mints a
// fresh token on the first call.
//
// Env vars (all required when calling these helpers):
//   ZOHO_DC              "com" | "eu" | "in" | "com.au" | "jp" | "sa" | "ca"
//   ZOHO_CLIENT_ID       Self-Client client_id (api-console.zoho.<dc>)
//   ZOHO_CLIENT_SECRET   Self-Client client_secret
//   ZOHO_REFRESH_TOKEN   long-lived refresh token from the OAuth exchange
//   ZOHO_ORG_ID          Books organization id (Settings → Organization Profile)
//
// Failure semantics: every helper throws on any non-2xx response.
// Callers (the Stripe webhook) MUST wrap calls in try/catch and
// log the error WITHOUT re-throwing - a Zoho outage must never
// cause us to fail the webhook and block the buyer's license email.

// ---- DC routing ---------------------------------------------------------

const ACCOUNTS_HOST: Record<string, string> = {
  com:    "accounts.zoho.com",
  eu:     "accounts.zoho.eu",
  in:     "accounts.zoho.in",
  "com.au": "accounts.zoho.com.au",
  jp:     "accounts.zoho.jp",
  sa:     "accounts.zoho.sa",
  ca:     "accounts.zohocloud.ca",
};

const API_HOST: Record<string, string> = {
  com:    "www.zohoapis.com",
  eu:     "www.zohoapis.eu",
  in:     "www.zohoapis.in",
  "com.au": "www.zohoapis.com.au",
  jp:     "www.zohoapis.jp",
  sa:     "www.zohoapis.sa",
  ca:     "www.zohoapis.ca",
};

function env(key: string): string {
  const v = process.env[key];
  if (!v) {
    throw new Error(
      `${key} is not set. The Zoho integration requires ZOHO_DC, ` +
        `ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID.`,
    );
  }
  return v;
}

function dc(): string {
  return env("ZOHO_DC").toLowerCase();
}

function accountsBase(): string {
  const d = dc();
  const host = ACCOUNTS_HOST[d];
  if (!host) throw new Error(`unknown ZOHO_DC: ${d}`);
  return `https://${host}`;
}

function apiBase(): string {
  const d = dc();
  const host = API_HOST[d];
  if (!host) throw new Error(`unknown ZOHO_DC: ${d}`);
  return `https://${host}/books/v3`;
}

// ---- token cache --------------------------------------------------------

interface CachedToken {
  accessToken: string;
  expiresAt:   number; // ms epoch
}

let cachedToken: CachedToken | null = null;

/**
 * Returns a valid Zoho access token, minting a fresh one via the
 * refresh_token if the cached one is expired or close to expiring.
 *
 * Tokens are good for 1 hour; we refresh with a 60s safety margin.
 */
export async function getAccessToken(): Promise<string> {
  if (cachedToken && cachedToken.expiresAt > Date.now() + 60_000) {
    return cachedToken.accessToken;
  }

  const url = `${accountsBase()}/oauth/v2/token`;
  const body = new URLSearchParams({
    grant_type:    "refresh_token",
    refresh_token: env("ZOHO_REFRESH_TOKEN"),
    client_id:     env("ZOHO_CLIENT_ID"),
    client_secret: env("ZOHO_CLIENT_SECRET"),
  });

  const res = await fetch(url, {
    method:  "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body:    body.toString(),
  });

  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`zoho token refresh ${res.status}: ${txt}`);
  }

  const json = (await res.json()) as {
    access_token?: string;
    expires_in?:   number;
    error?:        string;
  };

  if (!json.access_token) {
    throw new Error(
      `zoho token refresh: no access_token (${json.error ?? "unknown"})`,
    );
  }

  cachedToken = {
    accessToken: json.access_token,
    expiresAt:   Date.now() + ((json.expires_in ?? 3600) - 60) * 1000,
  };
  return cachedToken.accessToken;
}

// ---- low-level request helper ------------------------------------------

async function zohoFetch<T>(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path:   string,
  body?:  unknown,
): Promise<T> {
  const token = await getAccessToken();
  const orgId = env("ZOHO_ORG_ID");

  // Append organization_id as a query param on every request.
  const sep = path.includes("?") ? "&" : "?";
  const url = `${apiBase()}${path}${sep}organization_id=${orgId}`;

  const res = await fetch(url, {
    method,
    headers: {
      Authorization:  `Zoho-oauthtoken ${token}`,
      "Content-Type": "application/json",
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`zoho ${method} ${path}: ${res.status} ${txt}`);
  }
  return (await res.json()) as T;
}

// ---- Contacts -----------------------------------------------------------

interface ZohoContact {
  contact_id:   string;
  contact_name: string;
  email?:       string;
}
interface ZohoContactsResponse {
  contacts: ZohoContact[];
}
interface ZohoContactCreateResponse {
  contact: ZohoContact;
}

/**
 * Look up a Zoho contact by email; create one if it doesn't exist.
 * Returns the Zoho contact_id either way.
 *
 * The contact_name is set to the email (we don't have a real name
 * from Stripe Checkout in v1) and contact_persons.email is set to
 * the same address so Zoho's "send invoice by email" workflows have
 * a place to send to if the operator ever uses them.
 */
export async function findOrCreateContact(email: string): Promise<string> {
  const lowered = email.trim().toLowerCase();

  // Search first. Zoho's `email_contains` is a substring filter, so
  // we re-check the result for an exact match.
  const search = await zohoFetch<ZohoContactsResponse>(
    "GET",
    `/contacts?email_contains=${encodeURIComponent(lowered)}`,
  );
  const hit = (search.contacts ?? []).find(
    (c) => (c.email ?? "").toLowerCase() === lowered,
  );
  if (hit) return hit.contact_id;

  // Create.
  const created = await zohoFetch<ZohoContactCreateResponse>(
    "POST",
    "/contacts",
    {
      contact_name:    lowered,
      contact_type:    "customer",
      contact_persons: [
        {
          email:              lowered,
          first_name:         lowered.split("@")[0],
          is_primary_contact: true,
        },
      ],
    },
  );
  return created.contact.contact_id;
}

// ---- Invoices + Payments -----------------------------------------------

interface ZohoInvoice {
  invoice_id:        string;
  invoice_number:    string;
  customer_id:       string;
  reference_number?: string;
}
interface ZohoInvoiceCreateResponse {
  invoice: ZohoInvoice;
}
interface ZohoInvoicesResponse {
  invoices: ZohoInvoice[];
}

interface PurchaseArgs {
  /** Buyer email from Stripe checkout. */
  email: string;
  /** Order total in MAJOR units (e.g. 249.00 for USD 249). */
  amount: number;
  /** ISO 4217 currency code, uppercase (e.g. "USD"). */
  currency: string;
  /** Stripe Checkout Session id. Stored in invoice notes for trace. */
  sessionId: string;
  /** Stripe payment_intent id. Used as reference_number so refunds
   *  can find this invoice via search. */
  paymentIntentId: string;
}

/**
 * Create an Invoice + Customer Payment for a successful Stripe
 * checkout. Three Zoho API calls under the hood:
 *
 *   1. POST /invoices                      -> creates the invoice
 *   2. POST /invoices/{id}/status/sent     -> transitions Draft → Sent
 *      (required before recording payment)
 *   3. POST /customerpayments              -> records the cash and
 *      applies it to the invoice (which moves it Sent → Paid)
 *
 * `reference_number` on the invoice is the Stripe payment_intent id
 * so the refund handler can find it later. The Stripe session id
 * is stashed in `notes` for human trace.
 */
export async function createInvoiceForPurchase(
  args: PurchaseArgs,
): Promise<{ invoiceId: string }> {
  const contactId = await findOrCreateContact(args.email);

  // 1. Create invoice (Draft).
  const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
  const invoiceRes = await zohoFetch<ZohoInvoiceCreateResponse>(
    "POST",
    "/invoices",
    {
      customer_id:      contactId,
      reference_number: args.paymentIntentId,
      date:             today,
      line_items: [
        {
          name:        "Delfi License",
          description: "Autonomous Polymarket trader, lifetime license.",
          rate:        args.amount,
          quantity:    1,
        },
      ],
      notes: `Stripe session: ${args.sessionId}\nStripe PaymentIntent: ${args.paymentIntentId}`,
    },
  );
  const invoiceId = invoiceRes.invoice.invoice_id;

  // 2. Mark Draft → Sent. Zoho rejects payments against Draft invoices.
  //    If the org auto-marks new invoices as Sent (a setting), this
  //    call will return a "status already sent" 400; treat as success.
  try {
    await zohoFetch<unknown>("POST", `/invoices/${invoiceId}/status/sent`);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (!/already.*sent|status.*sent/i.test(msg)) {
      throw e;
    }
  }

  // 3. Record customer payment, applied to this invoice.
  await zohoFetch<unknown>("POST", "/customerpayments", {
    customer_id:      contactId,
    payment_mode:     "creditcard",
    amount:           args.amount,
    date:             today,
    reference_number: args.paymentIntentId,
    description:      `Stripe ${args.sessionId}`,
    invoices: [
      {
        invoice_id:     invoiceId,
        amount_applied: args.amount,
      },
    ],
  });

  return { invoiceId };
}

// ---- Refunds (Credit Notes) --------------------------------------------

interface ZohoCreditNoteCreateResponse {
  creditnote: { creditnote_id: string };
}

interface RefundArgs {
  /** Stripe payment_intent id; used to find the original invoice. */
  paymentIntentId: string;
  /** Refund amount in MAJOR units. */
  amount: number;
  /** ISO 4217 currency. */
  currency: string;
}

/**
 * Record a Stripe refund as a Zoho Credit Note against the
 * original invoice. Returns null if no matching invoice was found
 * (logged but non-fatal so the webhook still 200s).
 */
export async function recordRefund(
  args: RefundArgs,
): Promise<{ creditNoteId: string } | null> {
  const search = await zohoFetch<ZohoInvoicesResponse>(
    "GET",
    `/invoices?reference_number=${encodeURIComponent(args.paymentIntentId)}`,
  );
  const original = (search.invoices ?? [])[0];
  if (!original) {
    console.warn(
      `[zoho] no invoice found for refund (paymentIntent=${args.paymentIntentId})`,
    );
    return null;
  }

  const today = new Date().toISOString().slice(0, 10);
  const created = await zohoFetch<ZohoCreditNoteCreateResponse>(
    "POST",
    "/creditnotes",
    {
      customer_id:      original.customer_id,
      reference_number: args.paymentIntentId,
      date:             today,
      line_items: [
        {
          name:        "Delfi License (refund)",
          description: `Refund of invoice ${original.invoice_number}`,
          rate:        args.amount,
          quantity:    1,
        },
      ],
      notes: `Refund of invoice ${original.invoice_number} (Stripe PaymentIntent ${args.paymentIntentId}).`,
    },
  );

  return { creditNoteId: created.creditnote.creditnote_id };
}
