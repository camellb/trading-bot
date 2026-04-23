"use client";

import type { OpenPosition, SettledPosition } from "@/hooks/use-dashboard-data";
import { pnlColorClass, polymarketUrl, prob, sideColorClass, timeAgo, timeUntil, usd } from "@/lib/format";
import { DetailField } from "@/components/ui/DetailField";

import { Fragment, useState, type Dispatch, type SetStateAction } from "react";

type Tab = "all" | "open" | "settled";
type Direction = "asc" | "desc";
type OpenSortKey = "question" | "side" | "cost" | "entry" | "bot" | "ev" | "confidence" | "resolution";
type SettledSortKey = "question" | "side" | "outcome" | "cost" | "pnl" | "entry" | "settle" | "when";
type SortState<T extends string> = { key: T; direction: Direction };

const OPEN_DEFAULTS: Record<OpenSortKey, Direction> = {
  question: "asc", side: "asc", cost: "desc", entry: "desc",
  bot: "desc", ev: "desc", confidence: "desc", resolution: "asc",
};

const SETTLED_DEFAULTS: Record<SettledSortKey, Direction> = {
  question: "asc", side: "asc", outcome: "asc", cost: "desc", pnl: "desc",
  entry: "desc", settle: "desc", when: "desc",
};

function toggleSort<T extends string>(
  current: SortState<T>, key: T, defaults: Record<T, Direction>,
): SortState<T> {
  if (current.key === key) {
    return { key, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { key, direction: defaults[key] };
}

function compareText(a: string | null | undefined, b: string | null | undefined, d: Direction) {
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  return d === "asc" ? a.localeCompare(b) : b.localeCompare(a);
}

function compareNumber(a: number | null | undefined, b: number | null | undefined, d: Direction) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return d === "asc" ? a - b : b - a;
}

function parseTime(iso: string | null | undefined) {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? null : ms;
}

function compareTime(a: string | null | undefined, b: string | null | undefined, d: Direction) {
  return compareNumber(parseTime(a), parseTime(b), d);
}

function sortOpen(rows: OpenPosition[], sort: SortState<OpenSortKey>) {
  return [...rows].sort((a, b) => {
    let r = 0;
    switch (sort.key) {
      case "question":   r = compareText(a.question, b.question, sort.direction); break;
      case "side":       r = compareText(a.side, b.side, sort.direction); break;
      case "cost":       r = compareNumber(a.cost_usd, b.cost_usd, sort.direction); break;
      case "entry":      r = compareNumber(a.entry_price, b.entry_price, sort.direction); break;
      case "bot":        r = compareNumber(a.claude_probability, b.claude_probability, sort.direction); break;
      case "ev":         r = compareNumber(a.ev_bps, b.ev_bps, sort.direction); break;
      case "confidence": r = compareNumber(a.confidence, b.confidence, sort.direction); break;
      case "resolution": r = compareTime(a.expected_resolution_at, b.expected_resolution_at, sort.direction); break;
    }
    return r !== 0 ? r : b.id - a.id;
  });
}

function sortSettled(rows: SettledPosition[], sort: SortState<SettledSortKey>) {
  return [...rows].sort((a, b) => {
    let r = 0;
    switch (sort.key) {
      case "question": r = compareText(a.question, b.question, sort.direction); break;
      case "side":     r = compareText(a.side, b.side, sort.direction); break;
      case "outcome":  r = compareText(a.settlement_outcome, b.settlement_outcome, sort.direction); break;
      case "cost":     r = compareNumber(a.cost_usd, b.cost_usd, sort.direction); break;
      case "pnl":      r = compareNumber(a.realized_pnl_usd, b.realized_pnl_usd, sort.direction); break;
      case "entry":    r = compareNumber(a.entry_price, b.entry_price, sort.direction); break;
      case "settle":   r = compareNumber(a.settlement_price, b.settlement_price, sort.direction); break;
      case "when":     r = compareTime(a.settled_at, b.settled_at, sort.direction); break;
    }
    return r !== 0 ? r : b.id - a.id;
  });
}

function crowdPriceYes(row: { side: "YES" | "NO"; entry_price: number }) {
  return row.side === "YES" ? row.entry_price : 1 - row.entry_price;
}

function buildOpenBetSummary(row: OpenPosition): string {
  const entry = `$${row.entry_price.toFixed(3)}`;
  const crowd = prob(crowdPriceYes(row));
  const bot = prob(row.claude_probability);
  if (row.claude_probability == null) {
    return `Bought ${row.side} at ${entry}. Pays out if the market resolves ${row.side}.`;
  }
  if (row.side === "YES") {
    return `Bought YES at ${entry} because the bot estimate (${bot}) was above the crowd price (${crowd}).`;
  }
  return `Bought NO at ${entry} because the bot estimate (${bot}) was below the crowd price (${crowd}).`;
}

export function PositionsView({
  open, settled,
}: {
  open: OpenPosition[] | undefined;
  settled: SettledPosition[] | undefined;
}) {
  const [tab, setTab] = useState<Tab>("all");
  const [search, setSearch] = useState("");
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [openSort, setOpenSort] = useState<SortState<OpenSortKey>>({ key: "resolution", direction: "asc" });
  const [settledSort, setSettledSort] = useState<SortState<SettledSortKey>>({ key: "when", direction: "desc" });

  const openPositions = open ?? [];
  const settledPositions = settled ?? [];

  const lowerSearch = search.toLowerCase();
  const filteredOpen = openPositions.filter((p) =>
    !search || p.question.toLowerCase().includes(lowerSearch)
  );
  const filteredSettled = settledPositions.filter((p) =>
    !search || p.question.toLowerCase().includes(lowerSearch)
  );

  const sortedOpen = sortOpen(filteredOpen, openSort);
  const sortedSettled = sortSettled(filteredSettled, settledSort);

  const showOpen = tab === "all" || tab === "open";
  const showSettled = tab === "all" || tab === "settled";

  return (
    <div className="space-y-6 max-w-[1400px]">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white font-headline">Positions</h1>
          <p className="text-xs text-[#666] mt-1">
            {openPositions.length} open · {settledPositions.length} settled · click column to sort
          </p>
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3">
        <div className="flex bg-surface-2 border border-[#1a1a1a] p-0.5">
          {(["all", "open", "settled"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-1.5 text-xs font-medium transition-colors capitalize ${
                tab === t
                  ? "bg-accent text-surface-0"
                  : "text-[#a0a0a0] hover:text-white"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="relative flex-1 max-w-xs">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="absolute left-3 top-1/2 -translate-y-1/2 text-[#444]">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
          </svg>
          <input
            type="text"
            placeholder="Search markets..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-9 pr-3 py-2 text-xs bg-surface-2 border border-[#1a1a1a]
                       text-white placeholder-[#444] focus:outline-none focus:border-accent/50"
          />
        </div>
      </div>

      {/* Open Positions */}
      {showOpen && sortedOpen.length > 0 && (
        <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
          <div className="px-4 py-3 border-b border-[#1a1a1a]">
            <h3 className="text-xs uppercase tracking-widest text-[#a0a0a0] font-headline">Open Positions</h3>
          </div>
          <div className="overflow-x-auto max-h-[39rem]">
            <table className="w-full text-xs">
              <thead className="sticky top-0 z-10 bg-surface-2">
                <tr className="border-b border-[#1a1a1a] text-[10px] uppercase tracking-wider text-[#444] font-body">
                  <SortTh label="Market" sortKey="question" sort={openSort} setSort={setOpenSort} defaults={OPEN_DEFAULTS} />
                  <SortTh label="Side" sortKey="side" sort={openSort} setSort={setOpenSort} defaults={OPEN_DEFAULTS} center />
                  <SortTh label="Stake" sortKey="cost" sort={openSort} setSort={setOpenSort} defaults={OPEN_DEFAULTS} right />
                  <SortTh label="Entry" sortKey="entry" sort={openSort} setSort={setOpenSort} defaults={OPEN_DEFAULTS} right />
                  <SortTh label="Bot Est." sortKey="bot" sort={openSort} setSort={setOpenSort} defaults={OPEN_DEFAULTS} right className="hidden md:table-cell" />
                  <SortTh label="EV" sortKey="ev" sort={openSort} setSort={setOpenSort} defaults={OPEN_DEFAULTS} right className="hidden lg:table-cell" />
                  <SortTh label="Conf." sortKey="confidence" sort={openSort} setSort={setOpenSort} defaults={OPEN_DEFAULTS} right className="hidden lg:table-cell" />
                  <SortTh label="Resolves" sortKey="resolution" sort={openSort} setSort={setOpenSort} defaults={OPEN_DEFAULTS} right />
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1a1a1a]">
                {sortedOpen.map((p) => {
                  const isOpen = expandedId === p.id;
                  return (
                    <Fragment key={p.id}>
                      <tr
                        onClick={() => setExpandedId(isOpen ? null : p.id)}
                        className="hover:bg-surface-3/50 cursor-pointer transition-colors"
                      >
                        <td className="px-4 py-3">
                          <div className="flex items-start gap-2">
                            <span className="text-[#444] text-[10px] mt-0.5 shrink-0">{isOpen ? "▼" : "▶"}</span>
                            <div>
                              <span className={`text-white ${isOpen ? "whitespace-normal" : "line-clamp-1"}`}>{p.question}</span>
                              <div className="text-[10px] text-[#444]">#{p.id} · {p.category ?? "other"}</div>
                            </div>
                          </div>
                        </td>
                        <td className="px-3 py-3 text-center">
                          <span className={`inline-block px-2 py-0.5 text-[10px] font-semibold ${
                            p.side === "YES" ? "bg-accent-dim text-accent" : "bg-red-500/10 text-red-400"
                          }`}>{p.side}</span>
                        </td>
                        <td className="px-3 py-3 text-right font-body text-white">{usd(p.cost_usd)}</td>
                        <td className="px-3 py-3 text-right font-body text-[#ccc]">${p.entry_price.toFixed(2)}</td>
                        <td className="px-3 py-3 text-right font-body text-[#ccc] hidden md:table-cell">{prob(p.claude_probability)}</td>
                        <td className="px-3 py-3 text-right font-body text-[#a0a0a0] hidden lg:table-cell">
                          {p.ev_bps != null ? p.ev_bps.toFixed(0) : "-"}
                        </td>
                        <td className="px-3 py-3 text-right font-body text-[#a0a0a0] hidden lg:table-cell">
                          {p.confidence != null ? p.confidence.toFixed(2) : "-"}
                        </td>
                        <td className="px-3 py-3 text-right text-[#666] whitespace-nowrap">{timeUntil(p.expected_resolution_at)}</td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-surface-3/30">
                          <td colSpan={8} className="px-4 py-4">
                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-[11px] mb-3">
                              <DetailField label="Shares" value={p.shares.toFixed(1)} />
                              <DetailField label="Stake" value={usd(p.cost_usd)} />
                              <DetailField label="Entry price" value={`$${p.entry_price.toFixed(3)}`} />
                              <DetailField label="Bot estimate" value={prob(p.claude_probability)} />
                              <DetailField label="Crowd price" value={prob(crowdPriceYes(p))} />
                              <DetailField label="EV" value={p.ev_bps != null ? `${p.ev_bps.toFixed(0)} bps` : "-"} />
                              <DetailField label="Confidence" value={p.confidence?.toFixed(2) ?? "-"} />
                              <DetailField label="Opened" value={timeAgo(p.created_at)} />
                              <DetailField label="Resolves in" value={timeUntil(p.expected_resolution_at)} />
                            </div>
                            <div className="text-[10px] uppercase tracking-widest text-[#444] mb-1">Why this bet</div>
                            <div className="text-xs text-[#a0a0a0] whitespace-pre-wrap leading-relaxed mb-3">
                              {buildOpenBetSummary(p)}
                              {p.reasoning ? `\n\nBecause ${p.reasoning}` : ""}
                            </div>
                            <a
                              href={polymarketUrl(p.slug, p.market_id, p.event_slug)}
                              target="_blank" rel="noreferrer"
                              className="text-accent text-xs hover:text-accent-bright"
                              onClick={(e) => e.stopPropagation()}
                            >
                              Open on Polymarket &rarr;
                            </a>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Settled Positions */}
      {showSettled && sortedSettled.length > 0 && (
        <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
          <div className="px-4 py-3 border-b border-[#1a1a1a]">
            <h3 className="text-xs uppercase tracking-widest text-[#a0a0a0] font-headline">Settled Positions</h3>
          </div>
          <div className="overflow-x-auto max-h-[39rem]">
            <table className="w-full text-xs">
              <thead className="sticky top-0 z-10 bg-surface-2">
                <tr className="border-b border-[#1a1a1a] text-[10px] uppercase tracking-wider text-[#444] font-body">
                  <SortTh label="Market" sortKey="question" sort={settledSort} setSort={setSettledSort} defaults={SETTLED_DEFAULTS} />
                  <SortTh label="Side" sortKey="side" sort={settledSort} setSort={setSettledSort} defaults={SETTLED_DEFAULTS} center />
                  <SortTh label="Result" sortKey="outcome" sort={settledSort} setSort={setSettledSort} defaults={SETTLED_DEFAULTS} center />
                  <SortTh label="Stake" sortKey="cost" sort={settledSort} setSort={setSettledSort} defaults={SETTLED_DEFAULTS} right />
                  <SortTh label="P&L" sortKey="pnl" sort={settledSort} setSort={setSettledSort} defaults={SETTLED_DEFAULTS} right />
                  <SortTh label="Entry" sortKey="entry" sort={settledSort} setSort={setSettledSort} defaults={SETTLED_DEFAULTS} right className="hidden md:table-cell" />
                  <SortTh label="Settle" sortKey="settle" sort={settledSort} setSort={setSettledSort} defaults={SETTLED_DEFAULTS} right className="hidden md:table-cell" />
                  <SortTh label="When" sortKey="when" sort={settledSort} setSort={setSettledSort} defaults={SETTLED_DEFAULTS} right />
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1a1a1a]">
                {sortedSettled.map((p) => {
                  const won = p.settlement_outcome === p.side;
                  return (
                    <tr key={p.id} className="hover:bg-surface-3/50 transition-colors">
                      <td className="px-4 py-3">
                        <span className="text-[#ccc] line-clamp-1">{p.question}</span>
                        <div className="text-[10px] text-[#444]">#{p.id} · {p.category ?? "other"}</div>
                      </td>
                      <td className="px-3 py-3 text-center">
                        <span className={`inline-block px-2 py-0.5 text-[10px] font-semibold ${
                          p.side === "YES" ? "bg-accent-dim text-accent" : "bg-red-500/10 text-red-400"
                        }`}>{p.side}</span>
                      </td>
                      <td className="px-3 py-3 text-center">
                        <span className={`text-xs font-medium ${won ? "text-accent" : "text-red-400"}`}>
                          {won ? "WIN" : "LOSS"}
                        </span>
                      </td>
                      <td className="px-3 py-3 text-right font-body text-[#ccc]">{usd(p.cost_usd)}</td>
                      <td className={`px-3 py-3 text-right font-body ${pnlColorClass(p.realized_pnl_usd)}`}>
                        {usd(p.realized_pnl_usd, { sign: true })}
                      </td>
                      <td className="px-3 py-3 text-right font-body text-[#ccc] hidden md:table-cell">
                        ${p.entry_price.toFixed(3)}
                      </td>
                      <td className="px-3 py-3 text-right font-body text-[#ccc] hidden md:table-cell">
                        {p.settlement_price != null ? `$${p.settlement_price.toFixed(3)}` : "-"}
                      </td>
                      <td className="px-3 py-3 text-right text-[#666]">{timeAgo(p.settled_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Empty states */}
      {showOpen && sortedOpen.length === 0 && (
        <EmptyCard message="No open positions" />
      )}
      {showSettled && sortedSettled.length === 0 && (
        <EmptyCard message="No settled positions yet" />
      )}

    </div>
  );
}

/* ── Sub-components ─────────────────────────────────────────────────── */

function SortTh<T extends string>({
  label, sortKey, sort, setSort, defaults, right, center, className,
}: {
  label: string;
  sortKey: T;
  sort: SortState<T>;
  setSort: Dispatch<SetStateAction<SortState<T>>>;
  defaults: Record<T, Direction>;
  right?: boolean;
  center?: boolean;
  className?: string;
}) {
  const active = sort.key === sortKey;
  const indicator = active ? (sort.direction === "asc" ? "↑" : "↓") : "↕";
  const align = right ? "justify-end text-right" : center ? "justify-center text-center" : "justify-start text-left";

  return (
    <th className={`px-3 py-2.5 font-medium ${className ?? ""}`}>
      <button
        type="button"
        onClick={() => setSort((cur) => toggleSort(cur, sortKey, defaults))}
        className={`inline-flex w-full items-center gap-1 transition-colors hover:text-[#ccc] ${align}`}
      >
        <span>{label}</span>
        <span className={active ? "text-accent" : "text-[#333]"}>{indicator}</span>
      </button>
    </th>
  );
}

function EmptyCard({ message }: { message: string }) {
  return (
    <div className="bg-surface-2 border border-[#1a1a1a] px-4 py-12 text-center text-sm text-[#444]">
      {message}
    </div>
  );
}
