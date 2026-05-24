"use client";

import { useEffect, useState } from "react";

type TelegramStatus = { configured: boolean };

type NotificationPrefsResponse = {
  categories: string[];
  prefs: Record<string, boolean>;
};

const CATEGORY_LABELS: Record<string, { title: string; description: string }> = {
  position_opened: {
    title: "New positions",
    description:
      "Every time Delfi opens a position: market, side, stake, and forecast.",
  },
  position_settled: {
    title: "Position resolutions",
    description:
      "Every win or loss when a market resolves, with P&L and running bankroll.",
  },
  daily_summary: {
    title: "Daily summary",
    description: "End-of-day recap with trades, P&L, and record.",
  },
  weekly_summary: {
    title: "Weekly summary",
    description: "Weekly performance review with win rate and P&L.",
  },
  calibration: {
    title: "Calibration proposals",
    description:
      "When Delfi proposes a strategy change, with /apply and /reject controls.",
  },
  risk_event: {
    title: "Risk events",
    description:
      "Circuit breaker trips: daily loss cap, drawdown halt, or streak cooldown.",
  },
};

export default function NotificationsPage() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [reveal, setReveal] = useState(false);
  const [revealing, setRevealing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Per-category notification toggles. Missing keys default to on.
  const [prefCategories, setPrefCategories] = useState<string[]>([]);
  const [prefState, setPrefState] = useState<Record<string, boolean>>({});
  const [prefSavingKey, setPrefSavingKey] = useState<string | null>(null);
  const [prefError, setPrefError] = useState<string | null>(null);

  const toggleReveal = async () => {
    if (reveal) {
      setBotToken("");
      setChatId("");
      setReveal(false);
      return;
    }
    if (!configured) {
      setReveal(true);
      return;
    }
    setRevealing(true);
    setStatus(null);
    setError(null);
    try {
      const r = await fetch("/api/config/telegram/reveal", { cache: "no-store" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data?.error ?? `HTTP ${r.status}`);
      setBotToken(String(data?.bot_token ?? ""));
      setChatId(String(data?.chat_id ?? ""));
      setReveal(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRevealing(false);
    }
  };

  useEffect(() => {
    if (!status) return;
    const t = setTimeout(() => setStatus(null), 2500);
    return () => clearTimeout(t);
  }, [status]);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/config/telegram", { cache: "no-store" })
      .then((r) => r.json() as Promise<TelegramStatus>)
      .then((d) => {
        if (cancelled) return;
        setConfigured(Boolean(d?.configured));
      })
      .catch(() => {
        if (!cancelled) setConfigured(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Load notification prefs once on mount. Falls back silently: if the endpoint
  // isn't up yet (or the migration hasn't run) we render the default-all-on
  // state without an error splash.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/config/notifications", { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) return null;
        return (await r.json()) as NotificationPrefsResponse;
      })
      .then((d) => {
        if (cancelled || !d) return;
        setPrefCategories(Array.isArray(d.categories) ? d.categories : []);
        setPrefState(d.prefs && typeof d.prefs === "object" ? d.prefs : {});
      })
      .catch(() => {
        /* leave as defaults */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const isPrefOn = (key: string): boolean => {
    // Missing key == on. Explicit `false` == off.
    const v = prefState[key];
    return v === undefined ? true : Boolean(v);
  };

  const togglePref = async (key: string) => {
    const next = !isPrefOn(key);
    const previous = { ...prefState };
    // Optimistic update so the switch feels instant.
    setPrefState((s) => ({ ...s, [key]: next }));
    setPrefSavingKey(key);
    setPrefError(null);
    try {
      const res = await fetch("/api/config/notifications", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prefs: { ...prefState, [key]: next } }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data?.error ?? `HTTP ${res.status}`);
      }
      if (data?.prefs && typeof data.prefs === "object") {
        setPrefState(data.prefs);
      }
    } catch (err) {
      // Revert on failure.
      setPrefState(previous);
      setPrefError(err instanceof Error ? err.message : String(err));
    } finally {
      setPrefSavingKey(null);
    }
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setStatus(null);
    setError(null);
    try {
      const res = await fetch("/api/config/telegram", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_token: botToken.trim() || null,
          chat_id: chatId.trim() || null,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
      setConfigured(Boolean(data?.configured));
      setBotToken("");
      setChatId("");
      setStatus(data?.configured ? "Saved" : "Cleared");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const disconnect = async () => {
    setSaving(true);
    setStatus(null);
    setError(null);
    try {
      const res = await fetch("/api/config/telegram", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bot_token: null, chat_id: null }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
      setConfigured(false);
      setStatus("Disconnected.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const humanizeTelegramError = (raw: string): string => {
    const s = raw.toLowerCase();
    if (s.includes("chat not found")) {
      return "Telegram can't find that chat. Open your bot in Telegram, tap Start (or send any message), then copy the chat ID again from https://api.telegram.org/bot<TOKEN>/getUpdates. Group chats use a negative ID.";
    }
    if (s.includes("unauthorized")) {
      return "Telegram rejected the bot token. Double-check you copied the full token from @BotFather (including the colon).";
    }
    if (s.includes("bot was blocked") || s.includes("blocked by the user")) {
      return "You've blocked this bot in Telegram. Unblock it and send /start, then retry.";
    }
    if (s.includes("forbidden")) {
      return "Telegram refused delivery. For groups, make sure the bot is a member. For personal chats, message the bot first.";
    }
    return raw;
  };

  const sendTest = async () => {
    setTesting(true);
    setStatus(null);
    setError(null);
    try {
      const res = await fetch("/api/config/telegram/test", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
      setStatus("Test message sent - check your Telegram.");
    } catch (err) {
      const raw = err instanceof Error ? err.message : String(err);
      setError(humanizeTelegramError(raw));
    } finally {
      setTesting(false);
    }
  };

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Telegram</h2>
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <span className="panel-meta">
              {configured === null
                ? "Checking…"
                : configured
                ? "Connected"
                : "Not connected"}
            </span>
            {configured && (
              <button
                type="button"
                onClick={disconnect}
                disabled={saving}
                style={{
                  background: "transparent",
                  border: "none",
                  padding: 0,
                  cursor: saving ? "not-allowed" : "pointer",
                  color: "var(--vellum-60)",
                  fontFamily: "var(--font-mono)",
                  fontSize: 11,
                  letterSpacing: "0.14em",
                  textTransform: "uppercase",
                  textDecoration: "underline",
                  textUnderlineOffset: 3,
                }}
              >
                Disconnect
              </button>
            )}
          </div>
        </div>

        <p className="panel-body" style={{ marginTop: 0, marginBottom: 12 }}>
          Delfi sends every new position, every resolution, and daily &
          weekly summaries straight to your Telegram. Create a bot with{" "}
          <code>@BotFather</code>, paste its token below, message the bot
          once, then grab your chat ID from{" "}
          <code>
            https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates
          </code>
          .
        </p>

        <form onSubmit={save} className="form-row" style={{ gap: 12 }}>
          <div className="form-field">
            <label>
              Bot token
              {configured && (
                <span
                  style={{
                    marginLeft: 8,
                    color: "var(--vellum-60)",
                    fontSize: 12,
                  }}
                >
                  (saved)
                </span>
              )}
            </label>
            <input
              type={reveal ? "text" : "password"}
              autoComplete="off"
              placeholder={configured ? "••••••••" : "123456:ABC-…"}
              value={botToken}
              onChange={(e) => {
                setBotToken(e.target.value);
                setStatus(null);
                setError(null);
              }}
            />
            <div className="form-hint">Leave blank to keep the saved token.</div>
          </div>

          <div className="form-field">
            <label>Chat ID</label>
            <input
              type={reveal ? "text" : "password"}
              autoComplete="off"
              placeholder={configured ? "••••••••" : "e.g. 123456789"}
              value={chatId}
              onChange={(e) => {
                setChatId(e.target.value);
                setStatus(null);
                setError(null);
              }}
            />
            <div className="form-hint">
              Personal chat or a group - the bot must be a member.
            </div>
          </div>

          <div
            style={{
              marginTop: 12,
              display: "flex",
              gap: 10,
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            <button
              type="submit"
              className="btn-sm gold"
              disabled={saving || (!botToken.trim() && !chatId.trim())}
            >
              {saving ? "Saving…" : "Save credentials"}
            </button>
            <button
              type="button"
              className="btn-sm"
              onClick={toggleReveal}
              disabled={revealing}
            >
              {revealing ? "Loading…" : reveal ? "Hide values" : "Reveal values"}
            </button>
            {configured && (
              <button
                type="button"
                className="btn-sm"
                onClick={sendTest}
                disabled={testing}
              >
                {testing ? "Sending…" : "Send test message"}
              </button>
            )}
          </div>
          {(status || error) && (
            <div style={{ marginTop: 12, minHeight: 18 }}>
              {status && (
                <span style={{ color: "var(--vellum-60)", fontSize: 13 }}>
                  {status}
                </span>
              )}
              {error && (
                <span
                  style={{
                    color: "var(--red)",
                    fontSize: 13,
                    lineHeight: 1.5,
                  }}
                >
                  {error}
                </span>
              )}
            </div>
          )}
        </form>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">What Delfi will send</h2>
          <span className="panel-meta">
            {prefCategories.length > 0 ? "Per-category" : "All on"}
          </span>
        </div>
        <p
          className="panel-body"
          style={{ margin: 0, marginBottom: 12, color: "var(--vellum-60)" }}
        >
          Toggle individual categories on or off. Changes apply immediately.
        </p>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {(prefCategories.length > 0
            ? prefCategories
            : Object.keys(CATEGORY_LABELS)
          ).map((key) => {
            const label = CATEGORY_LABELS[key] ?? {
              title: key,
              description: "",
            };
            const on = isPrefOn(key);
            const saving = prefSavingKey === key;
            return (
              <label
                key={key}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  justifyContent: "space-between",
                  gap: 16,
                  padding: "12px 14px",
                  border: "1px solid var(--vellum-20)",
                  borderRadius: 6,
                  cursor: saving ? "wait" : "pointer",
                  opacity: saving ? 0.7 : 1,
                }}
              >
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <span style={{ fontWeight: 600, fontSize: 14 }}>
                    {label.title}
                  </span>
                  <span
                    style={{
                      color: "var(--vellum-60)",
                      fontSize: 13,
                      lineHeight: 1.45,
                    }}
                  >
                    {label.description}
                  </span>
                </div>
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => togglePref(key)}
                  disabled={saving}
                  role="switch"
                  aria-checked={on}
                  style={{
                    width: 20,
                    height: 20,
                    marginTop: 2,
                    cursor: saving ? "wait" : "pointer",
                    accentColor: "var(--gold, #c9a24b)",
                  }}
                />
              </label>
            );
          })}
        </div>
        {prefError && (
          <div
            style={{
              marginTop: 10,
              color: "var(--red)",
              fontSize: 13,
            }}
          >
            {prefError}
          </div>
        )}
      </div>
    </>
  );
}
