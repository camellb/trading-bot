import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, MarketEvaluation, PMPosition } from "../api";

/**
 * Positions page (desktop equivalent of /dashboard/positions).
 *
 * Four tabs:
 *   - All       : every PMPosition row in the current mode + every
 *                 evaluation that wasn't acted on (skipped) merged in.
 *   - Open      : status='open' positions, ordered by created_at desc.
 *   - Closed    : status='settled' positions, ordered by settled_at desc.
 *   - Skipped   : evaluations whose recommendation isn't YES/NO/BUY.
 *
 * Each row is expandable to reveal Delfi's reasoning and the entry
 * metadata (entry price, shares, expected resolution, M YES %, D YES %,
 * D CONF %). Reasoning is the same string the engine wrote when the
 * forecaster ran - we never paraphrase or re-summarize.
 */

type Tab = "all" | "open" | "closed" | "skipped";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "all",     label: "All" },
  { id: "open",    label: "Open" },
  { id: "closed",  label: "Closed" },
  { id: "skipped", label: "Skipped" },
];

export default function Positions() {
  const [tab, setTab] = useState<Tab>("all");
  const [positions, setPositions] = useState<PMPosition[]>([]);
  const [evaluations, setEvaluations] = useState<MarketEvaluation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [p, ev] = await Promise.all([
        api.positions(200).then((r) => r.positions),
        api.evaluations(200).then((r) => r.evaluations),
      ]);
      setPositions(p);
      setEvaluations(ev);
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15_000);
    return () => clearInterval(id);
  }, [refresh]);

  const open = useMemo(
    () => positions.filter((p) => p.status === "open"),
    [positions],
  );
  const closed = useMemo(
    () => positions.filter((p) => p.status === "settled"),
    [positions],
  );
  const skipped = useMemo(
    () => evaluations.filter((e) => {
      const rec = (e.recommendation ?? "").toUpperCase();
      return rec !== "YES" && rec !== "NO" && rec !== "BUY";
    }),
    [evaluations],
  );

  const counts = {
    all: open.length + closed.length + skipped.length,
    open: open.length,
    closed: closed.length,
    skipped: skipped.length,
  } as const;

  return (
    <>
      <header className="page-header">
        <h1>Positions</h1>
      </header>

      {error && <div className="error">{error}</div>}

      <nav className="subtabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`subtab ${tab === t.id ? "active" : ""}`}
            onClick={() => setTab(t.id)}
            type="button"
          >
            {t.label}
            <span className="count">{counts[t.id]}</span>
          </button>
        ))}
      </nav>

      {!loaded ? (
        <p className="empty">Loading...</p>
      ) : tab === "open" ? (
        open.length === 0 ? (
          <p className="empty">No open positions.</p>
        ) : (
          <PositionList positions={open} kind="open" />
        )
      ) : tab === "closed" ? (
        closed.length === 0 ? (
          <p className="empty">No closed positions yet.</p>
        ) : (
          <PositionList positions={closed} kind="closed" />
        )
      ) : tab === "skipped" ? (
        skipped.length === 0 ? (
          <p className="empty">No skipped evaluations recorded.</p>
        ) : (
          <SkippedList evaluations={skipped.slice(0, 100)} />
        )
      ) : (
        <AllList open={open} closed={closed} />
      )}
    </>
  );
}

// ── List for open + closed positions ───────────────────────────────────

function PositionList({
  positions,
  kind,
}: {
  positions: PMPosition[];
  kind: "open" | "closed";
}) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="positions-list">
      {positions.map((p) => (
        <PositionRow
          key={p.id}
          p={p}
          kind={kind}
          isOpen={expanded.has(p.id)}
          onToggle={() => toggle(p.id)}
        />
      ))}
    </div>
  );
}

function PositionRow({
  p,
  kind,
  isOpen,
  onToggle,
}: {
  p: PMPosition;
  kind: "open" | "closed";
  isOpen: boolean;
  onToggle: () => void;
}) {
  const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
  const mYesPct = Math.round(marketYes * 100);
  const dYesPct =
    p.claude_probability != null
      ? Math.round(p.claude_probability * 100)
      : null;
  const dConfPct =
    p.confidence != null ? Math.round(p.confidence * 100) : null;
  const pnl = p.realized_pnl_usd;
  const win = pnl != null && pnl >= 0;
  const reasoning = (p.reasoning ?? "").trim();
  const settlementOutcome = (p.settlement_outcome ?? "").toUpperCase();

  return (
    <>
      <div
        className={`position-row ${isOpen ? "expanded" : ""}`}
        onClick={onToggle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
      >
        <div className="position-q">
          <span className="q-text">{p.question}</span>
          <span className="q-meta">
            {p.market_archetype || p.category || "uncategorized"}
            {kind === "closed" && p.settled_at &&
              ` · settled ${new Date(p.settled_at).toLocaleDateString()}`}
            {kind === "open" && p.expected_resolution_at &&
              ` · resolves ${new Date(p.expected_resolution_at).toLocaleDateString()}`}
          </span>
        </div>
        <span className={`side-chip ${p.side}`}>{p.side}</span>
        <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
          ${p.cost_usd.toFixed(0)}
        </span>
        <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
          M {mYesPct}%
        </span>
        <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
          D {dYesPct != null ? `${dYesPct}%` : "-"}
        </span>
        {kind === "closed" ? (
          <span
            className="t-num"
            style={{
              fontSize: 13,
              color: win ? "var(--profit)" : "var(--ember)",
            }}
          >
            {pnl != null
              ? `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`
              : "-"}
          </span>
        ) : (
          <span
            className="t-num"
            style={{ fontSize: 12, color: "var(--vellum-60)" }}
          >
            {dConfPct != null ? `${dConfPct}% conf` : "-"}
          </span>
        )}
      </div>

      {isOpen && (
        <div className="position-detail">
          <div className="grid-3" style={{ marginBottom: 12 }}>
            <KV label="Entry price" value={p.entry_price.toFixed(3)} />
            <KV label="Shares" value={p.shares.toFixed(2)} />
            <KV label="Cost" value={`$${p.cost_usd.toFixed(2)}`} />
            <KV
              label={kind === "closed" ? "Settled" : "Opened"}
              value={
                kind === "closed"
                  ? p.settled_at
                    ? new Date(p.settled_at).toLocaleString()
                    : "-"
                  : new Date(p.created_at).toLocaleString()
              }
            />
            {kind === "closed" && (
              <>
                <KV
                  label="Outcome"
                  value={settlementOutcome || "-"}
                />
                <KV
                  label="Settle price"
                  value={
                    p.settlement_price != null
                      ? p.settlement_price.toFixed(3)
                      : "-"
                  }
                />
              </>
            )}
            {kind === "open" && (
              <KV
                label="Expected close"
                value={
                  p.expected_resolution_at
                    ? new Date(p.expected_resolution_at).toLocaleString()
                    : "-"
                }
              />
            )}
            {p.ev_bps != null && (
              <KV label="EV (bps)" value={p.ev_bps.toFixed(0)} />
            )}
          </div>
          <div>
            <div className="hero-label" style={{ marginBottom: 4 }}>
              Delfi's reasoning
            </div>
            <p style={{ margin: 0 }}>
              {reasoning || "No reasoning recorded for this entry."}
            </p>
          </div>
        </div>
      )}
    </>
  );
}

// ── Skipped evaluations list ───────────────────────────────────────────

function SkippedList({ evaluations }: { evaluations: MarketEvaluation[] }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="positions-list">
      {evaluations.map((e) => {
        const isOpen = expanded.has(e.id);
        const dYes =
          e.claude_probability != null
            ? Math.round(e.claude_probability * 100)
            : null;
        const mYes =
          e.market_price_yes != null
            ? Math.round(e.market_price_yes * 100)
            : null;
        const dConf =
          e.confidence != null ? Math.round(e.confidence * 100) : null;
        const reasoning = (e.reasoning ?? "").trim();
        const reasoningShort = (e.reasoning_short ?? "").trim();
        return (
          <React.Fragment key={e.id}>
            <div
              className={`position-row ${isOpen ? "expanded" : ""}`}
              onClick={() => toggle(e.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  toggle(e.id);
                }
              }}
              style={{
                gridTemplateColumns: "1fr auto auto auto auto auto",
                opacity: 0.85,
              }}
            >
              <div className="position-q">
                <span className="q-text">{e.question}</span>
                <span className="q-meta">
                  {e.market_archetype || e.category || "uncategorized"} ·{" "}
                  {new Date(e.evaluated_at).toLocaleString()}
                </span>
              </div>
              <span
                className="side-chip"
                style={{ color: "var(--vellum-60)" }}
              >
                SKIP
              </span>
              <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
                M {mYes != null ? `${mYes}%` : "-"}
              </span>
              <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
                D {dYes != null ? `${dYes}%` : "-"}
              </span>
              <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
                {dConf != null ? `${dConf}% conf` : "-"}
              </span>
              <span style={{ fontSize: 11, color: "var(--vellum-40)" }}>
                {(e.recommendation ?? "skip").toLowerCase()}
              </span>
            </div>

            {isOpen && (
              <div className="position-detail">
                {reasoningShort && (
                  <p style={{ margin: "0 0 10px" }}>
                    <strong style={{ color: "var(--vellum)" }}>
                      Why Delfi skipped:
                    </strong>{" "}
                    {reasoningShort}
                  </p>
                )}
                {reasoning && reasoning !== reasoningShort && (
                  <details>
                    <summary
                      style={{
                        cursor: "pointer",
                        color: "var(--gold)",
                        fontSize: 12,
                        marginBottom: 6,
                      }}
                    >
                      Show full reasoning
                    </summary>
                    <p style={{ margin: "8px 0 0", color: "var(--vellum-60)" }}>
                      {reasoning}
                    </p>
                  </details>
                )}
                {!reasoning && !reasoningShort && (
                  <p style={{ margin: 0, color: "var(--vellum-60)" }}>
                    No reasoning recorded for this evaluation.
                  </p>
                )}
              </div>
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

// ── "All" tab: chronological merge of open + closed ────────────────────

type Mixed = { ts: string; node: React.ReactNode };

function AllList({
  open,
  closed,
}: {
  open: PMPosition[];
  closed: PMPosition[];
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (k: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const items: Mixed[] = useMemo(() => {
    const list: Mixed[] = [];
    for (const p of open) {
      const key = `o-${p.id}`;
      list.push({
        ts: p.created_at ?? "",
        node: (
          <PositionRow
            key={key}
            p={p}
            kind="open"
            isOpen={expanded.has(key)}
            onToggle={() => toggle(key)}
          />
        ),
      });
    }
    for (const p of closed) {
      const key = `c-${p.id}`;
      list.push({
        ts: p.settled_at ?? p.created_at ?? "",
        node: (
          <PositionRow
            key={key}
            p={p}
            kind="closed"
            isOpen={expanded.has(key)}
            onToggle={() => toggle(key)}
          />
        ),
      });
    }
    return list.sort((a, b) => (a.ts < b.ts ? 1 : -1));
  }, [open, closed, expanded]);

  return <div className="positions-list">{items.map((i, idx) =>
    <React.Fragment key={idx}>{i.node}</React.Fragment>
  )}</div>;
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="hero-label">{label}</div>
      <div className="t-num" style={{ fontSize: 13, color: "var(--vellum)" }}>
        {value}
      </div>
    </div>
  );
}
