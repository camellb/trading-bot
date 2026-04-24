"use client";

import { useEffect, useState, useCallback } from "react";

// ── Connections (venue + dual-venue credentials) ──────────────────────────
// The onboarding flow chooses a venue and stores it on user_config.venue
// (Supabase). The Connections settings page lets users switch venue and
// fill in the credentials for whichever side they trade on. This hook
// fetches status for BOTH venues in one call so the UI can render the
// current venue AND show which side is ready for live trading.

export type Venue = "polymarket" | "polymarket_us";

export type OffshoreCreds = {
  apiKey: string;
  apiSecret: string;
  passphrase: string;
  walletAddress: string;
};

export type UsCreds = {
  apiKey: string;
  apiSecret: string;
  passphrase: string;
};

export const EMPTY_OFFSHORE: OffshoreCreds = {
  apiKey: "",
  apiSecret: "",
  passphrase: "",
  walletAddress: "",
};

export const EMPTY_US: UsCreds = {
  apiKey: "",
  apiSecret: "",
  passphrase: "",
};

export type OffshoreStatus = {
  apiKeySet: boolean;
  apiSecretSet: boolean;
  passphraseSet: boolean;
  walletAddress: string;
  readyForLive: boolean;
};

export type UsStatus = {
  apiKeySet: boolean;
  apiSecretSet: boolean;
  passphraseSet: boolean;
  readyForLive: boolean;
};

export type ConnectionsStatus = {
  venue: Venue;
  supportedVenues: Venue[];
  polymarket: OffshoreStatus;
  polymarketUs: UsStatus;
  readyForLive: boolean;
};

const EMPTY_OFFSHORE_STATUS: OffshoreStatus = {
  apiKeySet: false,
  apiSecretSet: false,
  passphraseSet: false,
  walletAddress: "",
  readyForLive: false,
};

const EMPTY_US_STATUS: UsStatus = {
  apiKeySet: false,
  apiSecretSet: false,
  passphraseSet: false,
  readyForLive: false,
};

export const EMPTY_CONNECTIONS_STATUS: ConnectionsStatus = {
  venue: "polymarket",
  supportedVenues: ["polymarket", "polymarket_us"],
  polymarket: EMPTY_OFFSHORE_STATUS,
  polymarketUs: EMPTY_US_STATUS,
  readyForLive: false,
};

type RawVenueStatus = {
  venue?: string;
  supported_venues?: string[];
  ready_for_live?: boolean;
  polymarket?: {
    api_key_set?: boolean;
    api_secret_set?: boolean;
    passphrase_set?: boolean;
    wallet_address?: string | null;
    ready_for_live?: boolean;
  };
  polymarket_us?: {
    api_key_set?: boolean;
    api_secret_set?: boolean;
    passphrase_set?: boolean;
    ready_for_live?: boolean;
  };
};

function isVenue(v: unknown): v is Venue {
  return v === "polymarket" || v === "polymarket_us";
}

function connectionsFromRaw(raw: RawVenueStatus | null | undefined): ConnectionsStatus {
  const venue = isVenue(raw?.venue) ? raw!.venue : "polymarket";
  const supported = (raw?.supported_venues ?? []).filter(isVenue);
  return {
    venue,
    supportedVenues:
      supported.length > 0 ? supported : ["polymarket", "polymarket_us"],
    polymarket: {
      apiKeySet:    !!raw?.polymarket?.api_key_set,
      apiSecretSet: !!raw?.polymarket?.api_secret_set,
      passphraseSet: !!raw?.polymarket?.passphrase_set,
      walletAddress: raw?.polymarket?.wallet_address ?? "",
      readyForLive: !!raw?.polymarket?.ready_for_live,
    },
    polymarketUs: {
      apiKeySet:    !!raw?.polymarket_us?.api_key_set,
      apiSecretSet: !!raw?.polymarket_us?.api_secret_set,
      passphraseSet: !!raw?.polymarket_us?.passphrase_set,
      readyForLive: !!raw?.polymarket_us?.ready_for_live,
    },
    readyForLive: !!raw?.ready_for_live,
  };
}

export function venueLabel(v: Venue): string {
  return v === "polymarket_us" ? "Polymarket US" : "Polymarket";
}

export function missingForVenue(status: ConnectionsStatus): string[] {
  const out: string[] = [];
  if (status.venue === "polymarket") {
    if (!status.polymarket.apiKeySet) out.push("Polymarket API key");
    if (!status.polymarket.apiSecretSet) out.push("Polymarket API secret");
    if (!status.polymarket.walletAddress.trim())
      out.push("Wallet address");
  } else {
    if (!status.polymarketUs.apiKeySet) out.push("Polymarket US API key");
    if (!status.polymarketUs.apiSecretSet) out.push("Polymarket US API secret");
  }
  return out;
}

// Save bundles: every field is optional so a save can touch venue only,
// one venue's creds only, or any combination. Empty string = clear (NULL
// in DB); undefined = leave untouched; non-empty = overwrite.
export type ConnectionsDraft = {
  venue?: Venue;
  polymarket?: Partial<OffshoreCreds>;
  polymarketUs?: Partial<UsCreds>;
};

function serializeDraft(draft: ConnectionsDraft): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (draft.venue !== undefined) body.venue = draft.venue;
  if (draft.polymarket) {
    const p = draft.polymarket;
    const off: Record<string, string> = {};
    if (p.apiKey !== undefined) off.api_key = p.apiKey;
    if (p.apiSecret !== undefined) off.api_secret = p.apiSecret;
    if (p.passphrase !== undefined) off.passphrase = p.passphrase;
    if (p.walletAddress !== undefined) off.wallet_address = p.walletAddress;
    if (Object.keys(off).length > 0) body.polymarket = off;
  }
  if (draft.polymarketUs) {
    const u = draft.polymarketUs;
    const us: Record<string, string> = {};
    if (u.apiKey !== undefined) us.api_key = u.apiKey;
    if (u.apiSecret !== undefined) us.api_secret = u.apiSecret;
    if (u.passphrase !== undefined) us.passphrase = u.passphrase;
    if (Object.keys(us).length > 0) body.polymarket_us = us;
  }
  return body;
}

export function useConnections() {
  const [status, setStatus] = useState<ConnectionsStatus>(EMPTY_CONNECTIONS_STATUS);
  const [hydrated, setHydrated] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch("/api/config/venue", { cache: "no-store" });
      if (!r.ok) {
        setStatus(EMPTY_CONNECTIONS_STATUS);
        return;
      }
      const raw = (await r.json()) as RawVenueStatus;
      setStatus(connectionsFromRaw(raw));
    } catch {
      setStatus(EMPTY_CONNECTIONS_STATUS);
    } finally {
      setHydrated(true);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const save = useCallback(
    async (draft: ConnectionsDraft) => {
      setSaving(true);
      setError(null);
      try {
        const body = serializeDraft(draft);
        if (Object.keys(body).length === 0) {
          setSaving(false);
          return true;
        }
        const r = await fetch("/api/config/venue", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const payload = await r.json().catch(() => ({}));
          setError(payload?.error ?? "Couldn't save - try again.");
          return false;
        }
        const raw = (await r.json()) as RawVenueStatus;
        setStatus(connectionsFromRaw(raw));
        return true;
      } catch (exc) {
        setError((exc as Error).message);
        return false;
      } finally {
        setSaving(false);
      }
    },
    [],
  );

  return {
    status,
    hydrated,
    saving,
    error,
    refresh,
    save,
    missing: missingForVenue(status),
    canGoLive: status.readyForLive,
  };
}

export type TelegramCreds = { botToken: string; chatId: string };

export const EMPTY_TELEGRAM: TelegramCreds = { botToken: "", chatId: "" };

export function useTelegramCredentials() {
  const [configured, setConfigured] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch("/api/config/telegram", { cache: "no-store" });
      if (!r.ok) {
        setConfigured(false);
        return;
      }
      const j = (await r.json()) as { configured?: boolean };
      setConfigured(!!j?.configured);
    } catch {
      setConfigured(false);
    } finally {
      setHydrated(true);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const save = useCallback(async (draft: TelegramCreds) => {
    setSaving(true);
    setError(null);
    try {
      const r = await fetch("/api/config/telegram", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_token: draft.botToken,
          chat_id: draft.chatId,
        }),
      });
      if (!r.ok) {
        const payload = await r.json().catch(() => ({}));
        setError(payload?.error ?? "Couldn't save - try again.");
        return false;
      }
      const j = (await r.json()) as { configured?: boolean };
      setConfigured(!!j?.configured);
      return true;
    } catch (exc) {
      setError((exc as Error).message);
      return false;
    } finally {
      setSaving(false);
    }
  }, []);

  return { configured, hydrated, saving, error, refresh, save };
}
