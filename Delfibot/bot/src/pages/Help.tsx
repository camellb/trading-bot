import { useState, type ReactNode } from "react";
import type { Credentials } from "../api";
import type { Page, SettingsTab } from "../App";

/**
 * Help and setup guides — top-level page.
 *
 * Three sections:
 *
 *   1. Setup checklist. Reads /api/credentials + /api/config (already
 *      polled by App.tsx and passed in as props) to show which
 *      integrations are connected and which still need attention.
 *
 *   2. Guides. Numbered walkthroughs for each setup step. Inline
 *      expand/collapse (no separate detail pages) so users scroll
 *      one document instead of clicking through.
 *
 *   3. Troubleshooting. Common error -> fix entries. Grows over time.
 *
 * Content is plain JSX rather than MDX. With ~7 guides the tooling
 * cost of MDX isn't worth it; switch when the page grows past
 * roughly 15 entries.
 */

interface Props {
  creds: Credentials | null;
  config: Record<string, unknown> | null;
  goto: (p: Page, tab?: SettingsTab) => void;
}

export default function Help({ creds, config, goto }: Props) {
  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Help</h1>
            <p className="page-sub">
              Set up Delfi, connect your integrations, and find fixes
              for common errors.
            </p>
          </div>
        </div>
      </div>

      <SetupChecklist creds={creds} config={config} goto={goto} />
      <Guides />
      <Troubleshooting />
      <AboutBlock />
    </div>
  );
}

// ── Setup checklist ──────────────────────────────────────────────────────

function SetupChecklist({
  creds,
  config,
  goto,
}: {
  creds: Credentials | null;
  config: Record<string, unknown> | null;
  goto: (p: Page, tab?: SettingsTab) => void;
}) {
  // Status sources. `creds` is the /api/credentials snapshot;
  // `config` is /api/config. Both poll on App.tsx's 5s tick so this
  // panel updates without a manual refresh.
  const c = (creds ?? {}) as Record<string, unknown>;
  const cfg = (config ?? {}) as Record<string, unknown>;

  const hasLicense    = c.has_license_key === true;
  const hasAnthropic  = c.has_anthropic_key === true || c.has_llm_key === true;
  const hasPmKey      = c.has_polymarket_key === true;
  const hasRelayerKey = c.has_polymarket_relayer_api_key === true;
  const hasGemini     = c.has_gemini_key === true;
  const hasTelegram   = c.has_telegram_token === true
                        || (typeof cfg.telegram_chat_id === "string"
                            && (cfg.telegram_chat_id as string).length > 0);
  const mode          = (cfg.mode as string) || "simulation";

  const rows = [
    {
      title: "License key",
      ok: hasLicense,
      required: true,
      done: "Active — Delfi is unlocked.",
      todo: "Paste the license key you received in your purchase email.",
      action: { label: "Open Settings → Account", to: () => goto("settings", "account") },
    },
    {
      title: "Anthropic API key",
      ok: hasAnthropic,
      required: true,
      done: "Connected — Delfi can call the forecaster.",
      todo: "Without this, Delfi can't evaluate markets. Free tier works.",
      action: { label: "Open Settings → Account", to: () => goto("settings", "account") },
    },
    {
      title: "Polymarket private key",
      ok: hasPmKey,
      required: mode === "live",
      done: "Connected — Delfi can place live orders.",
      todo: mode === "live"
        ? "Required to place real trades in live mode."
        : "Optional in simulation. Required if you switch to live.",
      action: { label: "Open Settings → Account", to: () => goto("settings", "account") },
    },
    {
      title: "Polymarket Relayer API key",
      ok: hasRelayerKey,
      required: false,
      done: "Connected — winning positions auto-redeem gaslessly.",
      todo: "Without this, Delfi knows you won but can't collect the payout automatically. You'd have to click Redeem on polymarket.com after every win.",
      action: { label: "Open Settings → Account", to: () => goto("settings", "account") },
    },
    {
      title: "Gemini API key (optional)",
      ok: hasGemini,
      required: false,
      done: "Connected — faster keyword extraction.",
      todo: "Optional. Used for fast keyword extraction and news pre-filtering. Without it, Delfi falls back to raw RSS titles (still works, just noisier).",
      action: { label: "Open Settings → Account", to: () => goto("settings", "account") },
    },
    {
      title: "Telegram notifications (optional)",
      ok: hasTelegram,
      required: false,
      done: "Connected — you'll get position and summary alerts.",
      todo: "Optional. Get Telegram messages on every new position, every win/loss, and a daily summary.",
      action: { label: "Open Settings → Notifications", to: () => goto("settings", "notifications") },
    },
  ];

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Setup checklist</h2>
        <span className="panel-meta">
          {rows.filter((r) => r.ok).length} of {rows.length} connected
        </span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Each row reads from your live config. Status updates within a
        few seconds of saving a credential.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {rows.map((r) => (
          <ChecklistRow key={r.title} row={r} />
        ))}
      </div>
    </div>
  );
}

function ChecklistRow({
  row,
}: {
  row: {
    title: string;
    ok: boolean;
    required: boolean;
    done: string;
    todo: string;
    action: { label: string; to: () => void };
  };
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
        background: row.ok ? "rgba(50, 180, 100, 0.06)" : "rgba(220, 160, 60, 0.06)",
        border: `1px solid ${row.ok ? "rgba(50, 180, 100, 0.25)" : "rgba(220, 160, 60, 0.22)"}`,
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
        <button className="btn small" onClick={row.action.to}>
          {row.action.label}
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
        ✓ DONE
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
        Step-by-step walkthroughs for every integration. Open any one
        to expand.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <GuideActivateLicense />
        <GuideAnthropic />
        <GuidePolymarketAccount />
        <GuidePolymarketKey />
        <GuideRelayerKey />
        <GuideTelegram />
        <GuideGemini />
      </div>
    </div>
  );
}

function GuideActivateLicense() {
  return (
    <Guide title="Activate your license" defaultOpen={false}>
      <p>
        Your license is what unlocks Delfi after purchase. You'll
        only do this once per machine.
      </p>
      <Step n={1} title="Find your license key">
        Check the email you received from the purchase. It contains a
        text block starting with <code>delfi-license-...</code>. The
        whole block is your key — copy it exactly.
      </Step>
      <Step n={2} title="Paste it into Delfi">
        Open <strong>Settings → Account</strong> in this app. Paste
        the license key into the <em>License key</em> field. Click
        Save.
      </Step>
      <Step n={3} title="Confirm activation">
        The Help page's "License key" checklist row should flip to
        ✓ DONE. If it stays as ⚠️, the key was malformed — check you
        copied the whole block including the trailing characters.
      </Step>
      <CommonIssues>
        <Issue title="License rejected as invalid">
          The license is signed offline with an Ed25519 key. A wrong
          or partial paste will fail signature verification with
          "license signature is invalid". Recopy from the original
          email and try again. If it still fails, contact support
          with your order ID.
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuideAnthropic() {
  return (
    <Guide title="Get an Anthropic API key">
      <p>
        Delfi's forecaster runs on Claude (Anthropic's model). You
        bring your own API key — Delfi never sees it after you
        paste it in.
      </p>
      <Step n={1} title="Open Anthropic Console">
        Go to{" "}
        <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">
          console.anthropic.com → API Keys
        </a>
        . If you don't have an account, create one (email is fine).
      </Step>
      <Step n={2} title="Create a new key">
        Click <strong>Create Key</strong>. Name it something like
        "Delfi". Copy the key — it starts with <code>sk-ant-...</code>.
        You'll only see the full key once, so paste it into Delfi
        immediately.
      </Step>
      <Step n={3} title="Paste into Delfi">
        Open <strong>Settings → Account</strong>, paste into{" "}
        <em>LLM API key</em>, Save. The Help checklist row flips
        to ✓ DONE.
      </Step>
      <Step n={4} title="(Optional) Fund the account">
        Anthropic gives a small free allowance to new accounts. For
        meaningful trading volume you'll want to add a credit card.
        Delfi typically uses $5-$20/month of API credit at default
        scan cadence — single-digit cents per market evaluated.
      </Step>
      <CommonIssues>
        <Issue title="'Could not resolve authentication method'">
          The key didn't save. Re-open Settings, check the LLM API
          key field shows <em>(stored)</em>, and try again.
        </Issue>
        <Issue title="Hitting rate limits">
          Default scan is 5 minutes; that's well under Anthropic's
          standard tier rate limits. If you see 429s, drop the scan
          frequency (not yet user-exposed — open a support ticket).
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuidePolymarketAccount() {
  return (
    <Guide title="Create a Polymarket account and fund it">
      <p>
        Required only if you want to trade with real money. Skip
        if you're staying in simulation mode.
      </p>
      <Step n={1} title="Go to polymarket.com">
        Click <strong>Log In</strong> in the top right. New users
        sign up with email — Polymarket creates a managed wallet
        for you behind the scenes.
      </Step>
      <Step n={2} title="Fund the wallet">
        Click <strong>Deposit</strong> on the Polymarket dashboard.
        Send USDC on Polygon (or use Polymarket's on-ramp from a
        debit card). Your funds end up at your "funder" address — a
        smart contract wallet, not your raw EOA.
      </Step>
      <Step n={3} title="Find your funder address">
        Go to <strong>polymarket.com → Settings → Profile</strong>.
        The <em>Address</em> shown there is your funder — that's the
        address that holds your trading balance.
      </Step>
      <p>
        Once your account is funded, continue to{" "}
        <em>Export your Polymarket private key</em> below.
      </p>
    </Guide>
  );
}

function GuidePolymarketKey() {
  return (
    <Guide title="Export your Polymarket private key">
      <p>
        Delfi needs your private key to sign orders on your behalf.
        The key never leaves your computer — it's stored in{" "}
        <code>secrets.json</code> under Delfi's app-data directory,
        not in any cloud.
      </p>
      <Step n={1} title="Open Polymarket settings">
        Go to{" "}
        <a href="https://polymarket.com/settings" target="_blank" rel="noreferrer">
          polymarket.com → Settings
        </a>
        .
      </Step>
      <Step n={2} title="Export the key">
        Look for an option labelled <em>Export Private Key</em> or{" "}
        <em>Reveal Private Key</em>. The exact wording depends on
        which wallet provider Polymarket assigned you (Magic.link,
        Privy, or self-custodied). You'll usually need to confirm
        your email or do a second-factor check before the key is
        shown.
      </Step>
      <Step n={3} title="Copy the key (carefully)">
        A 64-character hex string prefixed with <code>0x</code>. This
        is the only credential that controls real money on your
        account — anyone with this key can move your funds. Don't
        paste it into chat apps, screenshots, or anywhere except
        Delfi's Settings page.
      </Step>
      <Step n={4} title="Paste into Delfi">
        Open <strong>Settings → Account</strong>, paste into{" "}
        <em>Polymarket private key</em>. The wallet address auto-
        derives. Save.
      </Step>
      <CommonIssues>
        <Issue title="'maker address not allowed' on first live order">
          Polymarket's V2 CLOB has a different address registered as
          your trading signer than the key you exported. Fix: go
          to Polymarket → Settings → API Keys, generate fresh
          credentials for THIS wallet, and paste them into Delfi's
          three "Polymarket API key / secret / passphrase" fields
          (Settings → Account, below the private key).
        </Issue>
        <Issue title="My wallet doesn't show an Export option">
          Older Magic.link sessions may not expose the private key
          directly. Contact Polymarket support and ask for the
          wallet's seed phrase — you can derive the private key
          from that.
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuideRelayerKey() {
  return (
    <Guide title="Enable auto-redeem (Polymarket Relayer API key)">
      <p>
        Without this, Delfi sees you won and tells you about it but
        can't actually claim the payout — your winnings stay as
        unclaimed CTF tokens until you visit polymarket.com and
        click Redeem.
      </p>
      <p>
        The Relayer API key lets Delfi submit a gasless transaction
        through Polymarket's relayer. Polymarket pays the gas. You
        don't need to hold MATIC.
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
        . You must be logged in with the same account whose private
        key you pasted into Delfi.
      </Step>
      <Step n={2} title="Create a new key">
        Click <strong>Create New</strong>. A UUID like{" "}
        <code>019d9954-da86-75ba-9555-148591395124</code> appears.
        Click the copy icon next to it.
      </Step>
      <Step n={3} title="Paste it into Delfi">
        Open <strong>Settings → Account</strong>, paste the UUID
        into <em>Polymarket Relayer API key</em>, Save. From the
        next winner onwards, Delfi auto-redeems within 10 minutes
        of settlement.
      </Step>
      <CommonIssues>
        <Issue title="Relayer rejected with 401">
          The key was created with a different wallet than the one
          you have connected to Delfi. Generate a fresh key while
          logged in with the same wallet, paste it in, Save.
        </Issue>
        <Issue title="Position settled but never auto-redeemed">
          Check the Troubleshooting → "Winning position not
          auto-redeemed" entry below. Usually one of three things:
          no Relayer key set, key for a different wallet, or
          negative-risk market (not yet supported).
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuideTelegram() {
  return (
    <Guide title="Connect Telegram for notifications">
      <p>
        Optional. With Telegram set up you get a message on every
        new position, every win/loss, and a daily summary.
      </p>
      <Step n={1} title="Create a Telegram bot">
        Open Telegram, search for{" "}
        <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">
          @BotFather
        </a>
        , and send <code>/newbot</code>. Follow the prompts; pick a
        name and a username ending in <code>bot</code>. BotFather
        replies with an HTTP API token — copy the whole thing.
      </Step>
      <Step n={2} title="Send the bot a message">
        In Telegram, click your new bot's name (or search for it)
        and send any message (just <code>/start</code> is fine). This
        creates a chat the bot can post to.
      </Step>
      <Step n={3} title="Paste the token into Delfi">
        Open <strong>Settings → Notifications</strong>, paste the
        BotFather token into <em>Telegram bot token</em>, Save.
        Delfi automatically discovers your chat ID by reading the
        bot's updates feed — no manual chat-ID step.
      </Step>
      <Step n={4} title="Send a test message">
        Use the <strong>Send test</strong> button in the
        Notifications panel. If you receive it, you're done.
      </Step>
      <CommonIssues>
        <Issue title="Test message never arrives">
          You haven't sent the bot a message yet (step 2). The bot
          can only message chats it's been talked to first.
        </Issue>
      </CommonIssues>
    </Guide>
  );
}

function GuideGemini() {
  return (
    <Guide title="Add a Gemini API key (optional)">
      <p>
        Optional. Delfi uses Gemini for fast keyword extraction and
        news pre-filtering before sending material to the main
        forecaster. Without it, Delfi falls back to raw RSS titles —
        still works, just noisier inputs.
      </p>
      <Step n={1} title="Get a key">
        Go to{" "}
        <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">
          aistudio.google.com → API Keys
        </a>
        . Free tier is generous and covers Delfi's needs.
      </Step>
      <Step n={2} title="Paste into Delfi">
        Open <strong>Settings → Account</strong>, paste into{" "}
        <em>Gemini API key</em>, Save.
      </Step>
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
        Common errors and their fixes. Open any one to expand.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <Guide title="Bot keeps skipping every market">
          <p>
            Most common cause: stake math doesn't meet Polymarket's
            $1-and-5-share platform minimum. Open{" "}
            <strong>Risk controls → Sizing and limits</strong> and
            check:
          </p>
          <ul>
            <li>
              <strong>Max stake percentage</strong> toggle: at small
              live bankrolls (under ~$50), leave this OFF. The sizer
              will bump each order to whatever Polymarket actually
              accepts (max($1, 5 × ask)).
            </li>
            <li>
              <strong>Base stake</strong>: even with the cap off, base
              stake × bankroll has to clear the platform minimum at
              the favourite price. At $10 bankroll and 2% base, base
              stake is $0.20 — too low for any market priced above
              $0.20. Bot will bump it.
            </li>
            <li>
              <strong>Archetype skip list</strong> (Risk → Archetypes):
              you may have toggled off too many categories.
            </li>
          </ul>
        </Guide>

        <Guide title="'API state timed out after 30s' banner">
          <p>
            The sidecar daemon's accept loop got starved by heavy
            background work (the analyst's LLM scan can take 30-90s
            during a busy scan). Two layers handle this:
          </p>
          <ul>
            <li>
              Dashboard endpoints have a dedicated threadpool isolated
              from analyst work. You shouldn't see this banner in
              normal operation.
            </li>
            <li>
              If you do see it, click <strong>Restart Delfi</strong>{" "}
              on the banner. The button is bounded at ~15s shell time
              + 8s GUI reload, so it can't hang forever.
            </li>
          </ul>
          <p>
            If the banner keeps coming back after a Restart, check{" "}
            <strong>Settings → Diagnostics</strong> for stuck
            scheduler jobs.
          </p>
        </Guide>

        <Guide title="Restart Delfi button doesn't seem to do anything">
          <p>
            Worst case the Restart button times out internally and
            the GUI reloads after 8 seconds. If the daemon happened
            to be in a deep launchd-wedged state, the new spawn
            takes another 10-15s to bind its port — total ~25s. Wait
            and the dashboard reconnects.
          </p>
          <p>
            If after a full minute you're still stuck on
            "Restarting...", quit Delfi from the menu bar and
            relaunch from /Applications. The daemon runs under
            launchd and is unaffected — only the GUI shell needs to
            come back.
          </p>
        </Guide>

        <Guide title="Winning position not auto-redeemed">
          <p>Most likely causes, in order:</p>
          <ul>
            <li>
              <strong>No Relayer API key set.</strong> Check the
              Setup checklist at the top of this page. Without the
              key, Delfi has no way to submit a gasless redeem.
            </li>
            <li>
              <strong>Relayer key was created with a different
              wallet.</strong> Delete the key on polymarket.com,
              recreate it while logged in with the SAME wallet
              connected to Delfi, paste the new key into Delfi.
            </li>
            <li>
              <strong>Market is a "negative-risk" multi-outcome
              bundle.</strong> These use a different on-chain
              contract that Delfi doesn't redeem yet. You'll need
              to click Redeem on polymarket.com for this one.
              Future versions will handle it automatically.
            </li>
            <li>
              <strong>Polymarket's UMA oracle hasn't reported
              yet.</strong> Sports markets usually settle 1-3 hours
              after the game ends. Check Polymarket's market page —
              if it says "Resolution pending", just wait.
            </li>
          </ul>
        </Guide>

        <Guide title="Bot opened a position on a market that doesn't fit">
          <p>
            Check{" "}
            <strong>Risk controls → Archetypes</strong>. Each market
            type is tagged into an archetype (e.g. "crypto", "tennis",
            "sports_other"). Toggle off any archetype you don't want
            Delfi trading at all.
          </p>
          <p>
            For market-price-band filtering (e.g. "only trade YES
            markets between 60% and 90%"), use the per-archetype
            band controls in the same panel.
          </p>
        </Guide>

        <Guide title="Numbers in Telegram don't match the dashboard">
          <p>
            They should match now (as of v1.6). The notification
            reads actual fill values from the database, not the
            limit-order intent. The dashboard reads the same source.
            Bankroll is the live on-chain wallet balance (pUSD +
            USDC.e). Total equity is bankroll plus the cost of every
            open position.
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

// ── About ────────────────────────────────────────────────────────────────

function AboutBlock() {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">About</h2>
      </div>
      <p className="form-hint" style={{ marginTop: 8 }}>
        Delfi runs entirely on your machine. Your private keys, API
        keys, and trading history never leave the app-data directory
        on this device.
      </p>
      <p className="form-hint">
        Found a bug or need a guide that isn't here? Reply to your
        purchase email and we'll add it.
      </p>
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
