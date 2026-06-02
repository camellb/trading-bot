import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Credentials, type TelegramConfig } from "../api";
import type { Page, SettingsTab } from "../App";

/**
 * Help and setup guides. Top-level page.
 *
 * Sections:
 *   1. Setup checklist - grouped by purpose (Forecaster / Trading /
 *      Research / Alerts). Reads /api/credentials + /api/config polled
 *      by App.tsx, plus a one-shot /api/config/telegram fetch.
 *   2. Guides - inline expand/collapse walkthroughs for each step the
 *      user has to perform.
 *   3. Troubleshooting - common error / fix entries.
 *
 * Naming stays LLM-agnostic. The forecaster is "LLM" and the keyword
 * extractor is "Search LLM" - we link to instructions for each major
 * provider rather than pushing one.
 *
 * `anchor` is the deep-link target from Settings help-hints. When set,
 * the matching guide auto-opens and scrolls into view; clearAnchor()
 * runs after the scroll so back/forth navigation resets cleanly.
 */

interface Props {
  creds: Credentials | null;
  config: Record<string, unknown> | null;
  goto: (p: Page, tab?: SettingsTab, helpAnchor?: string) => void;
  anchor: string | null;
  clearAnchor: () => void;
}

export default function Help({ creds, config, goto, anchor, clearAnchor }: Props) {
  const [telegram, setTelegram] = useState<TelegramConfig | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      api.telegram()
        .then((t) => { if (!cancelled) setTelegram(t); })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return (
    <div className="page-wrap narrow">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Help</h1>
          </div>
        </div>
      </div>

      <SetupChecklist
        creds={creds}
        config={config}
        telegram={telegram}
        goto={goto}
      />
      <Guides anchor={anchor} clearAnchor={clearAnchor} />
      <Troubleshooting />
    </div>
  );
}

// ── Setup checklist ──────────────────────────────────────────────────────

function SetupChecklist({
  creds,
  config,
  telegram,
  goto,
}: {
  creds: Credentials | null;
  config: Record<string, unknown> | null;
  telegram: TelegramConfig | null;
  goto: (p: Page, tab?: SettingsTab, helpAnchor?: string) => void;
}) {
  const c = (creds ?? {}) as Record<string, unknown>;
  const cfg = (config ?? {}) as Record<string, unknown>;

  const hasLlm        = c.has_llm_key === true || c.has_anthropic_key === true;
  const hasLlmBackup  = c.has_llm_backup_key === true;
  const hasSearchLlm  = c.has_gemini_key === true; // server still keys this as "gemini"
  const hasPmKey      = c.has_polymarket_key === true;
  const hasRelayerKey = c.has_polymarket_relayer_api_key === true;
  const hasNewsapi    = c.has_newsapi_key === true;
  const hasCrypto     = c.has_cryptopanic_key === true;
  const hasTelegram   = !!(telegram && telegram.bot_token_configured
                            && telegram.chat_id);
  const mode          = (cfg.mode as string) || "simulation";

  const groups: Array<{ title: string; rows: ChecklistItem[] }> = [
    {
      title: "Forecaster",
      rows: [
        {
          title: "LLM API key",
          ok: hasLlm,
          required: true,
          done: "Connected.",
          todo: "BYO API key from any LLM provider.",
          actionTab: "connections",
        },
        {
          title: "Backup LLM API key",
          ok: hasLlmBackup,
          required: false,
          done: "Connected.",
          todo: "Second LLM used when the primary errors or rate-limits.",
          actionTab: "connections",
        },
        {
          title: "Search LLM",
          ok: hasSearchLlm,
          required: false,
          done: "Connected.",
          todo: "Used for keyword extraction and headline filtering. Cheap models recommended.",
          actionTab: "connections",
        },
      ],
    },
    {
      title: "Trading",
      rows: [
        {
          title: "Polymarket private key",
          ok: hasPmKey,
          required: mode === "live",
          done: "Connected.",
          todo: "Signs Polymarket orders in live mode.",
          actionTab: "connections",
        },
        {
          title: "Polymarket Relayer API key",
          ok: hasRelayerKey,
          required: false,
          done: "Connected.",
          todo: "Enables auto-redeem of winning positions.",
          actionTab: "connections",
        },
      ],
    },
    {
      title: "Research",
      rows: [
        {
          title: "NewsAPI key",
          ok: hasNewsapi,
          required: false,
          done: "Connected.",
          todo: "Headlines for geopolitical, economic, and current-event markets.",
          actionTab: "connections",
        },
        {
          title: "CryptoPanic key",
          ok: hasCrypto,
          required: false,
          done: "Connected.",
          todo: "Crypto-specific news for Polymarket crypto markets.",
          actionTab: "connections",
        },
      ],
    },
    {
      title: "Alerts",
      rows: [
        {
          title: "Telegram",
          ok: hasTelegram,
          required: false,
          done: "Connected.",
          todo: "Push positions, settlements, and summaries to your phone.",
          actionTab: "notifications",
        },
      ],
    },
  ];

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Setup checklist</h2>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
        {groups.map((g) => (
          <div key={g.title}>
            <div
              style={{
                fontSize: 12,
                fontWeight: 700,
                textTransform: "uppercase",
                letterSpacing: 0.6,
                opacity: 0.7,
                marginBottom: 10,
              }}
            >
              {g.title}
            </div>
            <div
              style={{ display: "flex", flexDirection: "column", gap: 10 }}
            >
              {g.rows.map((r) => (
                <ChecklistRow
                  key={r.title}
                  row={r}
                  onSetup={() => goto("settings", r.actionTab)}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

interface ChecklistItem {
  title: string;
  ok: boolean;
  required: boolean;
  done: string;
  todo: string;
  actionTab: SettingsTab;
}

function ChecklistRow({
  row,
  onSetup,
}: {
  row: ChecklistItem;
  onSetup: () => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        padding: "12px 16px",
        borderRadius: 10,
        background: row.ok
          ? "rgba(50, 180, 100, 0.06)"
          : "rgba(220, 160, 60, 0.06)",
        border: `1px solid ${row.ok
          ? "rgba(50, 180, 100, 0.25)"
          : "rgba(220, 160, 60, 0.22)"}`,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, marginBottom: 2 }}>{row.title}</div>
        <div className="form-hint" style={{ margin: 0 }}>
          {row.ok ? row.done : row.todo}
        </div>
      </div>
      {!row.ok && (
        <button className="btn small" onClick={onSetup}>
          Set up
        </button>
      )}
      <StatusPill ok={row.ok} required={row.required} />
    </div>
  );
}

function StatusPill({ ok, required }: { ok: boolean; required: boolean }) {
  if (ok) {
    return (
      <span
        style={{
          fontSize: 11,
          padding: "3px 8px",
          borderRadius: 12,
          background: "rgba(50, 180, 100, 0.18)",
          color: "rgb(120, 220, 160)",
          fontWeight: 600,
          letterSpacing: 0.5,
        }}
      >
        DONE
      </span>
    );
  }
  return (
    <span
      style={{
        fontSize: 11,
        padding: "3px 8px",
        borderRadius: 12,
        background: required
          ? "rgba(220, 100, 80, 0.18)"
          : "rgba(180, 180, 180, 0.14)",
        color: required ? "rgb(240, 170, 150)" : "rgb(190, 190, 190)",
        fontWeight: 600,
        letterSpacing: 0.5,
      }}
    >
      {required ? "REQUIRED" : "OPTIONAL"}
    </span>
  );
}

// ── Guides ───────────────────────────────────────────────────────────────

/** Canonical anchor IDs for deep-linking from Settings help-hints.
 *  The three LLM fields (primary, backup, search) all share the same
 *  guide because the steps are identical (create a key with any major
 *  provider, paste it into the matching field). */
export const HELP_ANCHORS = {
  llm: "llm-key",
  llmBackup: "llm-key",
  searchLlm: "llm-key",
  polymarketKey: "polymarket-key",
  polymarketRelayer: "polymarket-relayer",
  newsapi: "newsapi",
  cryptopanic: "cryptopanic",
  telegram: "telegram",
} as const;

function Guides({
  anchor,
  clearAnchor,
}: {
  anchor: string | null;
  clearAnchor: () => void;
}) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Guides</h2>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <GuideLlm anchor={anchor} clearAnchor={clearAnchor} />
        <GuidePolymarketKey anchor={anchor} clearAnchor={clearAnchor} />
        <GuideRelayerKey anchor={anchor} clearAnchor={clearAnchor} />
        <GuideNewsapi anchor={anchor} clearAnchor={clearAnchor} />
        <GuideCryptopanic anchor={anchor} clearAnchor={clearAnchor} />
        <GuideTelegram anchor={anchor} clearAnchor={clearAnchor} />
      </div>
    </div>
  );
}

// Reusable list of major LLM provider key pages. Stays provider-agnostic:
// no "recommended", no ordering implying preference, no commentary on
// cost or quality. Sorted alphabetically.
function LlmProviderLinks() {
  return (
    <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
      <li>
        <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">
          Anthropic
        </a>
      </li>
      <li>
        <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">
          Google Cloud / Vertex
        </a>
      </li>
      <li>
        <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">
          Google AI Studio (Gemini)
        </a>
      </li>
      <li>
        <a href="https://console.mistral.ai/api-keys" target="_blank" rel="noreferrer">
          Mistral
        </a>
      </li>
      <li>
        <a href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">
          OpenAI
        </a>
      </li>
      <li>
        <a href="https://console.x.ai/team/default/api-keys" target="_blank" rel="noreferrer">
          xAI (Grok)
        </a>
      </li>
    </ul>
  );
}

function GuideLlm({ anchor, clearAnchor }: GuideHookProps) {
  return (
    <Guide
      id={HELP_ANCHORS.llm}
      title="Connect an LLM"
      anchor={anchor}
      clearAnchor={clearAnchor}
    >
      <p>
        Delfi uses three LLM slots: the primary forecaster reads
        every market, the backup takes over when the primary errors
        or rate-limits, and the Search LLM does keyword extraction
        and headline filtering. The steps are the same for all
        three, only the field you paste into differs.
      </p>
      <Step n={1} title="Create an API key with your chosen provider">
        Provider key pages:
        <LlmProviderLinks />
      </Step>
      <Step n={2} title="Paste it into Delfi">
        Open <strong>Settings &rarr; Connections</strong> and paste
        into the matching field: <em>LLM API key</em> for the
        primary forecaster, <em>Backup LLM API key</em> for the
        fallback, or <em>Search LLM API key</em> for keyword
        extraction. Save.
      </Step>
      <Step n={3} title="Add billing at the provider">
        Most providers gate sustained usage behind a saved payment
        method. At default cadence Delfi costs single-digit cents
        per market evaluated.
      </Step>
    </Guide>
  );
}

function GuidePolymarketKey({ anchor, clearAnchor }: GuideHookProps) {
  return (
    <Guide
      id={HELP_ANCHORS.polymarketKey}
      title="Connect your Polymarket private key"
      anchor={anchor}
      clearAnchor={clearAnchor}
    >
      <p>
        Delfi signs Polymarket orders with the private key of the
        wallet that controls the Polymarket account. The key stays in
        the operating system keychain on this device.
      </p>
      <p>
        Everything else about the Polymarket account auto-derives from
        this key: wallet address, funder address, signature type, CLOB
        API credentials.
      </p>
      <Step n={1} title="Export the private key from Polymarket">
        Polymarket&apos;s docs on exporting the embedded wallet key:{" "}
        <a
          href="https://learn.polymarket.com/docs/guides/get-started/export-wallet"
          target="_blank"
          rel="noreferrer"
        >
          learn.polymarket.com &rarr; Export wallet
        </a>
        .
      </Step>
      <Step n={2} title="Treat the key like cash">
        It is a 64-character hex string starting with{" "}
        <code>0x</code>. Anyone with the key can move every dollar in
        the account. Do not paste it into chat apps, screenshots, or
        shared password managers.
      </Step>
      <Step n={3} title="Paste it into Delfi">
        Open <strong>Settings &rarr; Connections</strong> and paste
        into <em>Polymarket private key</em>. Save. The wallet
        address auto-fills.
      </Step>
      <CommonIssues>
        <Issue title="Polymarket account does not expose an export option">
          Older Magic.link sessions sometimes hide the export.
          Contact Polymarket support and ask them to walk through
          recovering the wallet seed phrase, then derive the private
          key from that.
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuideRelayerKey({ anchor, clearAnchor }: GuideHookProps) {
  return (
    <Guide
      id={HELP_ANCHORS.polymarketRelayer}
      title="Connect Polymarket Relayer API key"
      anchor={anchor}
      clearAnchor={clearAnchor}
    >
      <p>
        Enables auto-redeem of winning positions. Polymarket pays the
        gas through their relayer. Without this, winnings sit as
        unclaimed CTF tokens in the Polymarket balance until you click
        Redeem on polymarket.com.
      </p>
      <Step n={1} title="Open Polymarket Relayer API keys">
        <a
          href="https://polymarket.com/settings?tab=relayer-api-keys"
          target="_blank"
          rel="noreferrer"
        >
          polymarket.com &rarr; Settings &rarr; Relayer API keys
        </a>
        . Log in with the account whose private key is in Delfi.
      </Step>
      <Step n={2} title="Create a new key">
        Click <strong>Create New</strong>. A UUID like{" "}
        <code>019d9954-da86-75ba-9555-148591395124</code> appears.
        Copy it.
      </Step>
      <Step n={3} title="Paste it into Delfi">
        Open <strong>Settings &rarr; Connections</strong> and paste
        into <em>Polymarket Relayer API key</em>. Save.
      </Step>
      <CommonIssues>
        <Issue title="Relayer rejected with 401">
          The key was created on a different Polymarket account than
          the one whose private key is in Delfi. Delete the key on
          polymarket.com, log in with the right account, create a
          fresh one, paste it in.
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuideNewsapi({ anchor, clearAnchor }: GuideHookProps) {
  return (
    <Guide
      id={HELP_ANCHORS.newsapi}
      title="Connect NewsAPI"
      anchor={anchor}
      clearAnchor={clearAnchor}
    >
      <p>
        Pulls news headlines into research for geopolitical, economic,
        and current-event markets.
      </p>
      <Step n={1} title="Get a NewsAPI key">
        <a
          href="https://newsapi.org/register"
          target="_blank"
          rel="noreferrer"
        >
          newsapi.org &rarr; Register
        </a>
        . Free tier exists.
      </Step>
      <Step n={2} title="Paste it into Delfi">
        Open <strong>Settings &rarr; Connections</strong> and paste
        into <em>NewsAPI key</em>. Save.
      </Step>
    </Guide>
  );
}

function GuideCryptopanic({ anchor, clearAnchor }: GuideHookProps) {
  return (
    <Guide
      id={HELP_ANCHORS.cryptopanic}
      title="Connect CryptoPanic"
      anchor={anchor}
      clearAnchor={clearAnchor}
    >
      <p>
        Crypto-specific news (tokens, regulators, exchanges) for
        Polymarket crypto markets.
      </p>
      <Step n={1} title="Get a CryptoPanic key">
        <a
          href="https://cryptopanic.com/developers/api/"
          target="_blank"
          rel="noreferrer"
        >
          cryptopanic.com &rarr; API
        </a>
        . Free tier exists.
      </Step>
      <Step n={2} title="Paste it into Delfi">
        Open <strong>Settings &rarr; Connections</strong> and paste
        into <em>CryptoPanic key</em>. Save.
      </Step>
    </Guide>
  );
}

function GuideTelegram({ anchor, clearAnchor }: GuideHookProps) {
  return (
    <Guide
      id={HELP_ANCHORS.telegram}
      title="Connect Telegram"
      anchor={anchor}
      clearAnchor={clearAnchor}
    >
      <p>
        Push positions, settlements, and summaries to your phone.
      </p>
      <Step n={1} title="Create a Telegram bot">
        In Telegram, open{" "}
        <a
          href="https://t.me/BotFather"
          target="_blank"
          rel="noreferrer"
        >
          @BotFather
        </a>{" "}
        and send <code>/newbot</code>. BotFather returns an HTTP API
        token. Copy the full token.
      </Step>
      <Step n={2} title="Send the bot a message">
        Open the new bot&apos;s chat and send any message
        (<code>/start</code> works). This creates a chat the bot can
        post into. Delfi reads the chat id from the bot&apos;s updates
        feed automatically.
      </Step>
      <Step n={3} title="Paste the token into Delfi">
        Open <strong>Settings &rarr; Notifications</strong> and paste
        the BotFather token into <em>Telegram bot token</em>. Save.
      </Step>
      <Step n={4} title="Send a test message">
        Click <strong>Send test</strong> in the Notifications panel.
      </Step>
      <CommonIssues>
        <Issue title="Test message never arrives">
          The bot needs an initial message from you before it can
          post into a chat. Send any message to the bot in Telegram,
          then run Test again.
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

// ── Troubleshooting ──────────────────────────────────────────────────────

function Troubleshooting() {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Troubleshooting</h2>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <Guide title="Bot keeps skipping every market">
          <p>
            The most common cause is sizing math falling under the
            Polymarket platform minimum (every order must clear $1
            and 5 shares). Open <strong>Risk controls &rarr; Bet sizing
            and risk limits</strong> and check:
          </p>
          <ul>
            <li>
              <strong>Strict maximum bet size</strong>: at small live
              bankrolls (under roughly $50) leave this off. The
              sizer bumps each order to whatever Polymarket accepts.
            </li>
            <li>
              <strong>Default bet size</strong>: default bet size &times;
              bankroll has to clear the minimum at the
              favourite&apos;s price. At $10 bankroll and 2%, the
              default stake is $0.20, under the platform minimum.
            </li>
            <li>
              <strong>Archetype skip list</strong>: open Risk and
              confirm the per-archetype grid still has the
              categories you want enabled.
            </li>
          </ul>
        </Guide>

        <Guide title="Winning position not auto-redeemed">
          <ul>
            <li>
              <strong>No Relayer API key set.</strong> The setup
              checklist flags it. Without the key Delfi cannot
              submit a gasless redeem.
            </li>
            <li>
              <strong>Relayer key was created with the wrong
              account.</strong> The relayer rejects it as 401.
              Recreate it while logged in with the same Polymarket
              account whose private key is in Delfi.
            </li>
            <li>
              <strong>Negative-risk multi-outcome market.</strong>
              These use a different on-chain contract that Delfi
              does not redeem yet. Click Redeem on polymarket.com
              for these.
            </li>
            <li>
              <strong>Market not resolved on-chain yet.</strong>
              Polymarket&apos;s UMA oracle usually reports 1-3
              hours after the underlying event ends.
            </li>
          </ul>
        </Guide>

        <Guide title="'Delfi isn't responding' banner">
          <ul>
            <li>
              Click <strong>Restart Delfi</strong> on the banner.
              The restart is bounded, so it won't hang.
            </li>
            <li>
              If the banner keeps coming back, check{" "}
              <strong>Settings &rarr; Diagnostics</strong> for
              stuck scheduled jobs.
            </li>
          </ul>
        </Guide>

        <Guide title="Restart Delfi button seems stuck">
          <p>
            A restart can take up to 25 seconds before the
            dashboard reconnects. Wait the full window.
          </p>
          <p>
            If after a full minute the dashboard is still on
            &quot;Restarting...&quot;, quit Delfi from the macOS
            menu bar and relaunch it from /Applications. The bot
            itself keeps running in the background; only the
            dashboard window needs to come back.
          </p>
        </Guide>

        <Guide title="Numbers in Telegram do not match the dashboard">
          <p>
            Bankroll is the live on-chain wallet total (pUSD plus
            USDC.e). Total equity is bankroll plus the cost of every
            open position. Notifications read the actual fill value
            from the database and the dashboard reads the same
            source.
          </p>
          <p>
            If there is a mismatch, open a support ticket with
            screenshots of both surfaces.
          </p>
        </Guide>
      </div>
    </div>
  );
}

// ── Generic primitives ───────────────────────────────────────────────────

interface GuideHookProps {
  anchor: string | null;
  clearAnchor: () => void;
}

function Guide({
  id,
  title,
  children,
  defaultOpen = false,
  anchor,
  clearAnchor,
}: {
  id?: string;
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
  anchor?: string | null;
  clearAnchor?: () => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const ref = useRef<HTMLDivElement | null>(null);

  // Deep-link from Settings: when an anchor matches this Guide's id,
  // auto-open + scroll into view, then clear the anchor so navigating
  // away and back doesn't re-trigger.
  useEffect(() => {
    if (!id || !anchor || anchor !== id) return;
    setOpen(true);
    const el = ref.current;
    if (el) {
      requestAnimationFrame(() => {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
    clearAnchor?.();
  }, [anchor, id, clearAnchor]);

  return (
    <div
      ref={ref}
      style={{
        border: "1px solid rgba(255,255,255,0.07)",
        borderRadius: 8,
        background: "rgba(255,255,255,0.015)",
        scrollMarginTop: 80,
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          all: "unset",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          width: "100%",
          padding: "12px 16px",
          fontWeight: 600,
        }}
      >
        <span>{title}</span>
        <span
          style={{
            opacity: 0.6,
            fontSize: 14,
            transform: open ? "rotate(90deg)" : "rotate(0deg)",
            transition: "transform 0.15s",
          }}
        >
          &rsaquo;
        </span>
      </button>
      {open && (
        <div
          style={{
            padding: "0 16px 16px 16px",
            borderTop: "1px solid rgba(255,255,255,0.05)",
            fontSize: 14,
            lineHeight: 1.55,
          }}
        >
          {children}
        </div>
      )}
    </div>
  );
}

function Step({
  n,
  title,
  children,
}: {
  n: number;
  title: string;
  children: ReactNode;
}) {
  return (
    <div style={{ display: "flex", gap: 12, marginTop: 14 }}>
      <div
        style={{
          flexShrink: 0,
          width: 24,
          height: 24,
          borderRadius: 12,
          background: "rgba(120, 180, 220, 0.18)",
          color: "rgb(170, 210, 240)",
          display: "grid",
          placeItems: "center",
          fontSize: 12,
          fontWeight: 700,
        }}
      >
        {n}
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ fontWeight: 600, marginBottom: 2 }}>{title}</div>
        <div style={{ opacity: 0.85 }}>{children}</div>
      </div>
    </div>
  );
}

function CommonIssues({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        marginTop: 18,
        padding: 12,
        background: "rgba(220, 160, 60, 0.04)",
        border: "1px solid rgba(220, 160, 60, 0.18)",
        borderRadius: 8,
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          letterSpacing: 0.5,
          color: "rgb(220, 180, 120)",
          textTransform: "uppercase",
          marginBottom: 8,
        }}
      >
        Common issues
      </div>
      {children}
    </div>
  );
}

function Issue({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontWeight: 600, marginBottom: 2 }}>{title}</div>
      <div style={{ opacity: 0.85, fontSize: 13 }}>{children}</div>
    </div>
  );
}
