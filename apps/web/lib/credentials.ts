"use client";

import { useEffect, useState } from "react";

export type Credentials = {
  polymarketApiKey: string;
  polymarketApiSecret: string;
  polymarketPassphrase: string;
  walletAddress: string;
  telegramBotToken: string;
  telegramChatId: string;
};

export const EMPTY_CREDS: Credentials = {
  polymarketApiKey: "",
  polymarketApiSecret: "",
  polymarketPassphrase: "",
  walletAddress: "",
  telegramBotToken: "",
  telegramChatId: "",
};

const STORAGE_KEY = "delfi.credentials.v1";
const EVENT_NAME = "delfi:credentials-changed";

export function loadCredentials(): Credentials {
  if (typeof window === "undefined") return EMPTY_CREDS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY_CREDS;
    const parsed = JSON.parse(raw) as Partial<Credentials>;
    return { ...EMPTY_CREDS, ...parsed };
  } catch {
    return EMPTY_CREDS;
  }
}

export function saveCredentials(creds: Credentials) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(creds));
  window.dispatchEvent(new CustomEvent(EVENT_NAME));
}

export function requiredMissing(creds: Credentials): string[] {
  const missing: string[] = [];
  if (!creds.polymarketApiKey.trim()) missing.push("Polymarket API key");
  if (!creds.polymarketApiSecret.trim()) missing.push("Polymarket API secret");
  if (!creds.walletAddress.trim()) missing.push("Wallet address");
  return missing;
}

export function useCredentials() {
  const [creds, setCreds] = useState<Credentials>(EMPTY_CREDS);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setCreds(loadCredentials());
    setHydrated(true);
    const onChange = () => setCreds(loadCredentials());
    window.addEventListener(EVENT_NAME, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(EVENT_NAME, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  const update = (next: Credentials) => {
    setCreds(next);
    saveCredentials(next);
  };

  const missing = requiredMissing(creds);
  return { creds, update, hydrated, missing, canGoLive: missing.length === 0 };
}
