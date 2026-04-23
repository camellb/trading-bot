"use client";

import { useEffect, useState, useCallback } from "react";

export type PolymarketCreds = {
  apiKey: string;
  apiSecret: string;
  passphrase: string;
  walletAddress: string;
};

export const EMPTY_POLYMARKET: PolymarketCreds = {
  apiKey: "",
  apiSecret: "",
  passphrase: "",
  walletAddress: "",
};

// Server returns booleans instead of values so secrets never reach the browser.
export type PolymarketStatus = {
  apiKeySet: boolean;
  apiSecretSet: boolean;
  passphraseSet: boolean;
  walletAddress: string;
  readyForLive: boolean;
};

export const EMPTY_POLYMARKET_STATUS: PolymarketStatus = {
  apiKeySet: false,
  apiSecretSet: false,
  passphraseSet: false,
  walletAddress: "",
  readyForLive: false,
};

type RawGet = {
  api_key_set?: boolean;
  api_secret_set?: boolean;
  passphrase_set?: boolean;
  wallet_address?: string | null;
  ready_for_live?: boolean;
};

function statusFromRaw(raw: RawGet | null | undefined): PolymarketStatus {
  return {
    apiKeySet: !!raw?.api_key_set,
    apiSecretSet: !!raw?.api_secret_set,
    passphraseSet: !!raw?.passphrase_set,
    walletAddress: raw?.wallet_address ?? "",
    readyForLive: !!raw?.ready_for_live,
  };
}

export function missingForLive(status: PolymarketStatus): string[] {
  const out: string[] = [];
  if (!status.apiKeySet) out.push("Polymarket API key");
  if (!status.apiSecretSet) out.push("Polymarket API secret");
  if (!status.walletAddress.trim()) out.push("Wallet address");
  return out;
}

export function usePolymarketCredentials() {
  const [status, setStatus] = useState<PolymarketStatus>(EMPTY_POLYMARKET_STATUS);
  const [hydrated, setHydrated] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch("/api/config/polymarket", { cache: "no-store" });
      if (!r.ok) {
        setStatus(EMPTY_POLYMARKET_STATUS);
        return;
      }
      const raw = (await r.json()) as RawGet;
      setStatus(statusFromRaw(raw));
    } catch {
      setStatus(EMPTY_POLYMARKET_STATUS);
    } finally {
      setHydrated(true);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const save = useCallback(
    async (draft: PolymarketCreds) => {
      setSaving(true);
      setError(null);
      try {
        const body = {
          api_key: draft.apiKey,
          api_secret: draft.apiSecret,
          passphrase: draft.passphrase,
          wallet_address: draft.walletAddress,
        };
        const r = await fetch("/api/config/polymarket", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const payload = await r.json().catch(() => ({}));
          setError(payload?.error ?? "Couldn't save - try again.");
          return false;
        }
        const raw = (await r.json()) as RawGet;
        setStatus(statusFromRaw(raw));
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

  const missing = missingForLive(status);
  return {
    status,
    hydrated,
    saving,
    error,
    missing,
    canGoLive: status.readyForLive,
    refresh,
    save,
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
