"use client";

import { useEffect, useState } from "react";

type TelegramStatus = { configured: boolean };

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
          <span className="panel-meta">Automatic</span>
        </div>
        <ul className="panel-body" style={{ margin: 0, paddingLeft: 18 }}>
          <li>Every new position opened - market, side, stake, estimated probability.</li>
          <li>Every resolution - P&amp;L, win/loss, running bankroll.</li>
          <li>Daily summary at end of day - trades, P&amp;L, calibration.</li>
          <li>Weekly review - performance, Brier score, any proposed strategy changes.</li>
          <li>Risk events - daily loss cap, drawdown halt, or streak cooldown triggered.</li>
        </ul>
      </div>
    </>
  );
}
