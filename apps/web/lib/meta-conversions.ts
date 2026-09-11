import { createHash } from "node:crypto";

const GRAPH_API_VERSION = "v26.0";

interface MetaPurchaseArgs {
  eventId: string;
  eventTime: number;
  email: string;
  value: number;
  currency: string;
  sourceUrl: string;
  clientIp?: string;
  clientUserAgent?: string;
  fbc?: string;
  fbp?: string;
}

export interface MetaConversionResult {
  sent: boolean;
  eventsReceived?: number;
  reason?: "not_configured";
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

export async function sendMetaPurchase(
  args: MetaPurchaseArgs,
): Promise<MetaConversionResult> {
  const pixelId = process.env.META_PIXEL_ID || process.env.NEXT_PUBLIC_META_PIXEL_ID;
  const accessToken = process.env.META_CONVERSIONS_API_ACCESS_TOKEN;
  if (!pixelId || !accessToken) return { sent: false, reason: "not_configured" };

  const userData: Record<string, string | string[]> = {
    em: [sha256(args.email.trim().toLowerCase())],
  };
  if (args.clientIp) userData.client_ip_address = args.clientIp;
  if (args.clientUserAgent) userData.client_user_agent = args.clientUserAgent;
  if (args.fbc) userData.fbc = args.fbc;
  if (args.fbp) userData.fbp = args.fbp;

  const payload: Record<string, unknown> = {
    data: [
      {
        event_name: "Purchase",
        event_time: args.eventTime,
        event_source_url: args.sourceUrl,
        event_id: args.eventId,
        action_source: "website",
        user_data: userData,
        custom_data: {
          currency: args.currency,
          value: args.value,
          order_id: args.eventId,
          content_ids: ["delfi-personal-v1"],
          content_type: "product",
        },
      },
    ],
  };

  if (process.env.META_TEST_EVENT_CODE) {
    payload.test_event_code = process.env.META_TEST_EVENT_CODE;
  }

  const response = await fetch(
    `https://graph.facebook.com/${GRAPH_API_VERSION}/${encodeURIComponent(pixelId)}/events`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(8_000),
    },
  );

  const body = (await response.json().catch(() => ({}))) as {
    events_received?: number;
    error?: { message?: string };
  };
  if (!response.ok) {
    throw new Error(body.error?.message || `Meta returned HTTP ${response.status}`);
  }

  return { sent: true, eventsReceived: body.events_received };
}
