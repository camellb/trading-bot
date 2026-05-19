import { useEffect, useState, type ReactNode } from "react";
import { api, type Credentials, type TelegramConfig } from "../api";
import type { Page, SettingsTab } from "../App";

/**
 * Help and setup guides. Top-level page.
 *
 * Two sections:
 *
 *   1. Setup checklist. Grouped by purpose (Forecaster / Trading /
 *      Research / Alerts) so a fresh install sees what's missing
 *      without scrolling through a flat list. Reads /api/credentials
 *      and /api/config (already polled by App.tsx) plus a one-shot
 *      /api/config/telegram fetch for the Telegram row.
 *
 *   2. Guides. Inline expand/collapse walkthroughs for each step the
 *      user actually has to perform. Things Delfi auto-derives
 *      (wallet address, the polymarket api-key/secret/passphrase
 *      trio that the CLOB issues on first login) are NOT documented
 *      as setup steps; only as troubleshooting entries when they
 *      need a manual override.
 *
 *   3. Troubleshooting. Common error → fix entries.
 *
 * What this file deliberately does NOT cover:
 *   - License activation. The whole app is gated behind LicenseGate;
 *     anyone who can see this page already activated their license.
 *   - Polymarket account creation. The user already has one; if
 *     they didn't, they wouldn't have a private key to paste.
 *
 * Naming: the app is LLM-agnostic. The forecaster is "LLM", not
 * "Anthropic" or "Claude". The keyword extractor is "Search LLM"
 * (Gemini is the recommendation, not the only option).
 */

interface Props {
  creds: Credentials | null;
  config: Record<string, unknown> | null;
  goto: (p: Page, tab?: SettingsTab) => void;
}

export default function Help({ creds, config, goto }: Props) {
  // Telegram lives on a separate endpoint (chat-id is discovered server-side
  // from getUpdates, not stored alongside other creds). One-shot fetch on
  // mount + re-fetch every 15s so the checklist row updates after the user
  // saves a token.
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
            <p className="page-sub">
              Connect your integrations and find fixes for common errors.
            </p>
          </div>
        </div>
      </div>

      <SetupChecklist
        creds={creds}
        config={config}
        telegram={telegram}
        goto={goto}
      />
      <Guides />
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
  goto: (p: Page, tab?: SettingsTab) => void;
}) {
  const c = (creds ?? {}) as Record<string, unknown>;
  const cfg = (config ?? {}) as Record<string, unknown>;

  // Source of truth: the booleans returned by /api/credentials. See
  // Delfibot/bot/local_api.py: `_read_all()` builds this set.
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

  // Grouped by purpose so a fresh user reads it top-down: get the
  // forecaster running, then connect trading, then optionally improve
  // research and alerts.
  const groups: Array<{
    title: string;
    blurb: string;
    rows: ChecklistItem[];
  }> = [
    {
      title: "Forecaster",
      blurb: "The model Delfi uses to evaluate each market.",
      rows: [
        {
          title: "LLM API key",
          ok: hasLlm,
          required: true,
          done: "Connected. Delfi can evaluate markets.",
          todo: "Bring your own key from any major LLM provider. Required for Delfi to do anything.",
          actionTab: "connections",
        },
        {
          title: "Backup LLM API key",
          ok: hasLlmBackup,
          required: false,
          done: "Connected. Delfi falls back to this if the primary errors or rate-limits.",
          todo: "Optional. A second LLM Delfi falls back to on errors or rate limits.",
          actionTab: "connections",
        },
        {
          title: "Search LLM",
          ok: hasSearchLlm,
          required: false,
          done: "Connected. Keyword extraction is fast.",
          todo: "Optional. Cheap fast model used for keyword extraction and headline filtering. Gemini recommended (generous free tier).",
          actionTab: "connections",
        },
      ],
    },
    {
      title: "Trading",
      blurb: "Polymarket access. Required for live mode.",
      rows: [
        {
          title: "Polymarket private key",
          ok: hasPmKey,
          required: mode === "live",
          done: "Connected. Delfi can sign orders. The wallet address auto-derives.",
          todo: mode === "live"
            ? "Required for live mode. The private key of the wallet that controls your Polymarket account."
            : "Optional in simulation. Required if you switch to live.",
          actionTab: "connections",
        },
        {
          title: "Polymarket Relayer API key",
          ok: hasRelayerKey,
          required: false,
          done: "Connected. Winning positions auto-redeem with no MATIC needed.",
          todo: "Optional but recommended. Without this Delfi can't claim winnings automatically; you'd click Redeem on Polymarket yourself after every win.",
          actionTab: "connections",
        },
      ],
    },
    {
      title: "Research",
      blurb: "Extra context for the forecaster. All optional.",
      rows: [
        {
          title: "NewsAPI key",
          ok: hasNewsapi,
          required: false,
          done: "Connected. Breaking news headlines feed into market evaluations.",
          todo: "Optional. Pulls news headlines into research for geopolitical, economic, and current-event markets. Free tier at newsapi.org.",
          actionTab: "connections",
        },
        {
          title: "CryptoPanic key",
          ok: hasCrypto,
          required: false,
          done: "Connected. Crypto news feeds into research.",
          todo: "Optional. Crypto-specific news (tokens, regulators, exchanges) for Polymarket crypto markets. Free at cryptopanic.com.",
          actionTab: "connections",
        },
      ],
    },
    {
      title: "Alerts",
      blurb: "Optional notifications.",
      rows: [
        {
          title: "Telegram",
          ok: hasTelegram,
          required: false,
          done: "Connected. You'll get position and summary alerts.",
          todo: "Optional. Get Telegram messages on every new position, every win or loss, and a daily summary.",
          actionTab: "notifications",
        },
      ],
    },
  ];

  const totalRows = groups.reduce((n, g) => n + g.rows.length, 0);
  const okCount   = groups.reduce(
    (n, g) => n + g.rows.filter((r) => r.ok).length,
    0,
  );

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Setup checklist</h2>
        <span className="panel-meta">{okCount} of {totalRows} connected</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Status updates within a few seconds of saving a credential.
      </p>
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
                marginBottom: 8,
              }}
            >
              {g.title}
            </div>
            <p
              className="form-hint"
              style={{ marginTop: 0, marginBottom: 10 }}
            >
              {g.blurb}
            </p>
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
        display: "grid",
        gridTemplateColumns: "auto 1fr auto",
        gap: 14,
        alignItems: "center",
        padding: "12px 14px",
        borderRadius: 10,
        background: row.ok
          ? "rgba(50, 180, 100, 0.06)"
          : "rgba(220, 160, 60, 0.06)",
        border: `1px solid ${row.ok
          ? "rgba(50, 180, 100, 0.25)"
          : "rgba(220, 160, 60, 0.22)"}`,
      }}
    >
      <StatusPill ok={row.ok} required={row.required} />
      <div>
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

function Guides() {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Guides</h2>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Step-by-step walkthroughs for each connector.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <GuideLlm />
        <GuideBackupLlm />
        <GuideSearchLlm />
        <GuidePolymarketKey />
        <GuideRelayerKey />
        <GuideNews />
        <GuideTelegram />
      </div>
    </div>
  );
}

function GuideLlm() {
  return (
    <Guide title="Connect your LLM (required)">
      <p>
        Delfi's forecaster runs on whichever LLM you connect. The key
        is stored in your operating system keychain and never leaves
        your machine. Recommended provider: Anthropic Claude. Any
        major provider works as long as it exposes a Messages API.
      </p>
      <Step n={1} title="Get an API key from your provider">
        For Anthropic, go to{" "}
        <a
          href="https://console.anthropic.com/settings/keys"
          target="_blank"
          rel="noreferrer"
        >
          console.anthropic.com → API Keys
        </a>{" "}
        and click <strong>Create Key</strong>. You'll only see the
        full key once.
      </Step>
      <Step n={2} title="Paste it into Delfi">
        Open <strong>Settings → Connections</strong> and paste the
        key into the <em>LLM API key</em> field. Save.
      </Step>
      <Step n={3} title="(Optional) Add a credit card to the provider">
        Anthropic's free allowance is small. At default scan cadence
        Delfi typically spends single-digit cents per market
        evaluated; add a card at the provider if you plan to run for
        more than a day or two.
      </Step>
    </Guide>
  );
}

function GuideBackupLlm() {
  return (
    <Guide title="Add a backup LLM (optional)">
      <p>
        If your primary LLM rate-limits or errors out, Delfi will fall
        back to this one for the same evaluation. Useful at higher
        trading volume and as a hedge against single-provider outages.
        Pick a different provider from your primary (Anthropic and
        OpenAI both work).
      </p>
      <Step n={1} title="Get a key from a second provider">
        Pick anything that's not your primary. The key is stored the
        same way (keychain only).
      </Step>
      <Step n={2} title="Paste it into Delfi">
        Open <strong>Settings → Connections</strong> and paste into{" "}
        <em>Backup LLM API key</em>. Save.
      </Step>
    </Guide>
  );
}

function GuideSearchLlm() {
  return (
    <Guide title="Add a Search LLM (optional)">
      <p>
        The Search LLM is a smaller, cheaper model Delfi uses for
        keyword extraction and headline pre-filtering before sending
        material to the main forecaster. Recommended provider:{" "}
        <strong>Google Gemini</strong>. Generous free tier, fast.
        Without a Search LLM, Delfi falls back to raw RSS titles
        (still works, just noisier inputs).
      </p>
      <Step n={1} title="Get a Gemini key">
        Go to{" "}
        <a
          href="https://aistudio.google.com/app/apikey"
          target="_blank"
          rel="noreferrer"
        >
          aistudio.google.com → API Keys
        </a>{" "}
        and create one. Free tier covers Delfi's needs.
      </Step>
      <Step n={2} title="Paste it into Delfi">
        Open <strong>Settings → Connections</strong> and paste into
        the Search LLM field. Save.
      </Step>
    </Guide>
  );
}

function GuidePolymarketKey() {
  return (
    <Guide title="Connect your Polymarket trading key (required for live)">
      <p>
        Delfi signs Polymarket orders with the private key of the
        wallet that controls your Polymarket account. The key stays
        in your operating system keychain on this device. Skip this
        step if you only want to run in simulation.
      </p>
      <p>
        Everything else about your Polymarket account auto-derives
        from this key: wallet address, the funder address that holds
        your trading balance, your signature type, and your CLOB
        API credentials. You won't need to copy any of those by hand.
      </p>
      <Step n={1} title="Export the private key from Polymarket">
        Polymarket gives you a wallet when you sign up. To export the
        private key, go to{" "}
        <a
          href="https://polymarket.com/settings"
          target="_blank"
          rel="noreferrer"
        >
          polymarket.com → Settings
        </a>{" "}
        and look for an <em>Export private key</em> or{" "}
        <em>Reveal private key</em> option. The exact wording depends
        on which wallet provider Polymarket assigned to your account
        (Magic.link or Privy on most accounts). You'll usually need
        to confirm your email or pass a 2FA check before the key is
        shown.
      </Step>
      <Step n={2} title="Treat it like cash">
        The key is a 64-character hex string starting with{" "}
        <code>0x</code>. Anyone with this key can move every dollar
        in your Polymarket account. Don't paste it into chat apps,
        screenshots, password managers you share, or anywhere except
        Delfi's Settings page on this device.
      </Step>
      <Step n={3} title="Paste it into Delfi">
        Open <strong>Settings → Connections</strong> and paste it
        into the <em>Polymarket private key</em> field. Save. The
        wallet address auto-fills and you're done.
      </Step>
      <CommonIssues>
        <Issue title="My Polymarket account doesn't expose an export option">
          Older Magic.link sessions sometimes hide the export.
          Contact Polymarket support and ask them to walk you through
          retrieving the wallet's seed phrase, then derive the
          private key from that. See{" "}
          <a
            href="https://learn.polymarket.com"
            target="_blank"
            rel="noreferrer"
          >
            learn.polymarket.com
          </a>{" "}
          for their current help articles.
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuideRelayerKey() {
  return (
    <Guide title="Enable auto-redeem (Polymarket Relayer API key)">
      <p>
        Without this, Delfi sees that you won and tells you about it
        but can't actually claim the payout. Your winnings sit as
        unclaimed CTF tokens in your Polymarket balance until you
        visit polymarket.com and click Redeem.
      </p>
      <p>
        The Relayer API key lets Delfi submit a gasless transaction
        through Polymarket's relayer. Polymarket pays the gas. You
        don't need MATIC on your wallet.
      </p>
      <Step n={1} title="Open Polymarket → Relayer API keys">
        Go to{" "}
        <a
          href="https://polymarket.com/settings?tab=relayer-api-keys"
          target="_blank"
          rel="noreferrer"
        >
          polymarket.com → Settings → Relayer API keys
        </a>
        . Log in with the same account whose private key you pasted
        into Delfi.
      </Step>
      <Step n={2} title="Create a new key">
        Click <strong>Create New</strong>. A UUID like{" "}
        <code>019d9954-da86-75ba-9555-148591395124</code> appears.
        Copy it.
      </Step>
      <Step n={3} title="Paste it into Delfi">
        Open <strong>Settings → Connections</strong> and paste it
        into <em>Polymarket Relayer API key</em>. Save. From the next
        winner on, Delfi auto-redeems within 10 minutes of resolution.
      </Step>
      <CommonIssues>
        <Issue title="Relayer rejected with 401">
          The Relayer key was created on a different Polymarket
          account than the one whose private key you pasted into
          Delfi. Delete the key on polymarket.com, log in with the
          right account, create a fresh one, paste it in.
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuideNews() {
  return (
    <Guide title="Add news sources (optional)">
      <p>
        News feeds add late-breaking context to Delfi's research.
        Both are optional: without them Delfi falls back to RSS,
        which still works.
      </p>
      <Step n={1} title="NewsAPI for general news">
        Sign up at{" "}
        <a
          href="https://newsapi.org/register"
          target="_blank"
          rel="noreferrer"
        >
          newsapi.org
        </a>
        . Free tier is enough for personal use. Paste the key into{" "}
        <strong>Settings → Connections → NewsAPI key</strong>. Save.
      </Step>
      <Step n={2} title="CryptoPanic for crypto news">
        Sign up at{" "}
        <a
          href="https://cryptopanic.com/developers/api/"
          target="_blank"
          rel="noreferrer"
        >
          cryptopanic.com → API
        </a>
        . Paste the key into <strong>Settings → Connections →
        CryptoPanic key</strong>. Save. This is the source Delfi
        leans on for Polymarket crypto markets (BTC thresholds, ETH
        ETF, exchange events).
      </Step>
    </Guide>
  );
}

function GuideTelegram() {
  return (
    <Guide title="Connect Telegram for notifications (optional)">
      <p>
        Optional. With Telegram set up you get a message on every new
        position, every win or loss, and a daily summary.
      </p>
      <Step n={1} title="Create a Telegram bot">
        Open Telegram, search for{" "}
        <a
          href="https://t.me/BotFather"
          target="_blank"
          rel="noreferrer"
        >
          @BotFather
        </a>
        , and send <code>/newbot</code>. Follow the prompts. BotFather
        replies with an HTTP API token. Copy the whole token.
      </Step>
      <Step n={2} title="Send the bot a message">
        In Telegram, open the new bot's chat (search for its
        username) and send any message (<code>/start</code> works).
        This creates a chat the bot can post into. Delfi reads the
        chat ID from the bot's updates feed automatically; you don't
        need to copy anything else.
      </Step>
      <Step n={3} title="Paste the token into Delfi">
        Open <strong>Settings → Notifications</strong> and paste the
        BotFather token into <em>Telegram bot token</em>. Save.
      </Step>
      <Step n={4} title="Send a test message">
        Use the <strong>Send test</strong> button in the
        Notifications panel. If you get the message in Telegram,
        you're done.
      </Step>
      <CommonIssues>
        <Issue title="Test message never arrives">
          You haven't sent the bot a message yet (step 2). The bot
          can only post to chats that started the conversation first.
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
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Common errors and their fixes.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <Guide title="Bot keeps skipping every market">
          <p>
            The most common cause is sizing math falling under the
            Polymarket platform minimum (every order must clear $1 and
            5 shares). Open <strong>Risk controls → Sizing and
            limits</strong> and check:
          </p>
          <ul>
            <li>
              <strong>Max stake percentage</strong> toggle: at small
              live bankrolls (under roughly $50) leave this off. The
              sizer will bump each order to whatever Polymarket
              actually accepts.
            </li>
            <li>
              <strong>Base stake</strong>: base stake × bankroll has
              to clear the minimum at the favourite's price. At $10
              bankroll and 2% base, base stake is $0.20, under the
              platform minimum, so most markets get skipped.
            </li>
            <li>
              <strong>Archetype skip list</strong>: open Risk → the
              per-archetype grid and confirm you haven't toggled off
              the categories you actually want Delfi trading.
            </li>
          </ul>
        </Guide>

        <Guide title="'maker address not allowed' or 'the order signer address has to be the address of the API KEY'">
          <p>
            Polymarket's CLOB has a different address registered as
            your trading signer than the one Delfi auto-derives from
            your private key. This happens occasionally on accounts
            that were created via the web with an older session key.
          </p>
          <p>
            Fix: go to{" "}
            <a
              href="https://polymarket.com/settings?tab=api-keys"
              target="_blank"
              rel="noreferrer"
            >
              polymarket.com → Settings → API Keys
            </a>
            , generate fresh credentials while logged in with the
            same wallet whose private key is in Delfi, then paste the
            three values (api-key, secret, passphrase) into the
            matching override fields in{" "}
            <strong>Settings → Connections</strong>. Normally Delfi
            auto-derives these, so they only need to be set as a
            manual override.
          </p>
        </Guide>

        <Guide title="Winning position not auto-redeemed">
          <p>Most likely causes, in order:</p>
          <ul>
            <li>
              <strong>No Relayer API key set.</strong> The setup
              checklist at the top of this page flags it. Without
              the key Delfi has no way to submit a gasless redeem.
            </li>
            <li>
              <strong>Relayer key was created with the wrong
              account.</strong> If the key was created while logged
              in with a different Polymarket account, the relayer
              rejects it as 401. Recreate it while logged in with
              the same account whose private key is in Delfi.
            </li>
            <li>
              <strong>Negative-risk multi-outcome market.</strong>
              These use a different on-chain contract that Delfi
              doesn't redeem yet. Click Redeem on polymarket.com
              for this one.
            </li>
            <li>
              <strong>Market hasn't resolved on-chain yet.</strong>
              Polymarket's UMA oracle usually reports 1-3 hours
              after the underlying event ends. If the market page
              still says <em>Resolution pending</em>, wait.
            </li>
          </ul>
        </Guide>

        <Guide title="'API state timed out after 30s' banner">
          <p>
            The sidecar daemon's accept loop got starved by heavy
            background work. Dashboard endpoints have a dedicated
            threadpool isolated from the analyst, so this shouldn't
            happen in steady state. If it does:
          </p>
          <ul>
            <li>
              Click <strong>Restart Delfi</strong> on the banner.
              The restart has hard timeouts on every shelled command;
              it can't hang forever.
            </li>
            <li>
              If the banner keeps coming back after a restart, check{" "}
              <strong>Settings → Diagnostics</strong> for stuck
              scheduler jobs.
            </li>
          </ul>
        </Guide>

        <Guide title="Restart Delfi button seems stuck">
          <p>
            Worst case the restart shell-command set takes ~15 seconds
            and the GUI reload waits another 8 seconds, total around
            23 seconds before the dashboard reconnects. Wait for the
            full window.
          </p>
          <p>
            If after a full minute you're still on "Restarting...",
            quit Delfi from the macOS menu bar and relaunch from
            /Applications. The daemon runs under launchd and is
            unaffected; only the GUI shell needs to come back.
          </p>
        </Guide>

        <Guide title="Numbers in Telegram don't match the dashboard">
          <p>
            They should match: notifications read the actual fill
            value from the database (not the limit-order intent) and
            the dashboard reads the same source. Bankroll is the live
            on-chain wallet total (pUSD plus USDC.e). Total equity is
            bankroll plus the cost of every open position.
          </p>
          <p>
            If you do see a mismatch, please open a support ticket
            with screenshots of both surfaces.
          </p>
        </Guide>
      </div>
    </div>
  );
}

// ── Generic primitives ───────────────────────────────────────────────────

function Guide({
  title,
  children,
  defaultOpen = false,
}: {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div
      style={{
        border: "1px solid rgba(255,255,255,0.07)",
        borderRadius: 8,
        background: "rgba(255,255,255,0.015)",
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
          ›
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
