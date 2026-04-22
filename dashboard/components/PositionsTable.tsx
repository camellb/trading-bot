"use client";

import type {
  OpenPosition, SettledPosition,
} from "@/hooks/use-dashboard-data";
import {
  pnlColorClass, polymarketUrl, prob, sharePrice, sideColorClass, timeAgo, timeUntil, usd,
} from "@/lib/format";
import { Detail, Empty, Th } from "@/components/ui/Table";
import {
  Fragment, useMemo, useState, type Dispatch, type ReactNode, type SetStateAction,
} from "react";

type Direction = "asc" | "desc";
type OpenSortKey = "question" | "side" | "cost" | "entry" | "bot" | "ev" | "confidence" | "resolution";
type SettledSortKey = "question" | "side" | "outcome" | "pnl" | "entry" | "settle" | "when";
type SortState<T extends string> = { key: T; direction: Direction };

const OPEN_DEFAULTS: Record<OpenSortKey, Direction> = {
  question: "asc",
  side: "asc",
  cost: "desc",
  entry: "desc",
  bot: "desc",
  ev: "desc",
  confidence: "desc",
  resolution: "asc",
};

const SETTLED_DEFAULTS: Record<SettledSortKey, Direction> = {
  question: "asc",
  side: "asc",
  outcome: "asc",
  pnl: "desc",
  entry: "desc",
  settle: "desc",
  when: "desc",
};

export function PositionsTable({
  open, settled,
}: {
  open: OpenPosition[] | undefined;
  settled: SettledPosition[] | undefined;
}) {
  const [tab, setTab] = useState<"open" | "settled">("open");
  const [openSort, setOpenSort] = useState<SortState<OpenSortKey>>({
    key: "resolution",
    direction: "asc",
  });
  const [settledSort, setSettledSort] = useState<SortState<SettledSortKey>>({
    key: "when",
    direction: "desc",
  });

  return (
    <section className="border border-[#1a1a1a] bg-[#050505]">
      <header className="flex items-center justify-between px-3 py-2 border-b border-[#1a1a1a]">
        <div>
          <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0]">positions</h2>
          <div className="text-[10px] text-[#444]">click a column to sort</div>
        </div>
        <div className="flex gap-1 text-[11px]">
          <TabButton active={tab === "open"} onClick={() => setTab("open")}>
            open · {open?.length ?? 0}
          </TabButton>
          <TabButton active={tab === "settled"} onClick={() => setTab("settled")}>
            settled · {settled?.length ?? 0}
          </TabButton>
        </div>
      </header>
      <div className="max-h-[39rem] overflow-auto">
        {tab === "open" ? (
          <OpenRows rows={open} sort={openSort} setSort={setOpenSort} />
        ) : (
          <SettledRows rows={settled} sort={settledSort} setSort={setSettledSort} />
        )}
      </div>
    </section>
  );
}

function TabButton({
  active, onClick, children,
}: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-1 transition ${
        active
          ? "bg-[#0a0a0a] text-white"
          : "text-[#666] hover:text-[#ccc]"
      }`}
    >
      {children}
    </button>
  );
}

function Side({ side }: { side: "YES" | "NO" }) {
  return <span className={`font-body text-xs ${sideColorClass(side)}`}>{side}</span>;
}

function crowdPriceYes(row: { side: "YES" | "NO"; entry_price: number }) {
  return row.side === "YES" ? row.entry_price : 1 - row.entry_price;
}

function buildOpenBetSummary(row: OpenPosition): string {
  const entry = sharePrice(row.entry_price);
  const crowd = prob(crowdPriceYes(row));
  const bot = prob(row.claude_probability);

  if (row.claude_probability == null) {
    return `This bet buys ${row.side} at ${entry}. It pays out if the market resolves ${row.side}.`;
  }

  if (row.side === "YES") {
    return `This bet buys YES at ${entry}. It pays out if the market resolves YES because the bot estimate for YES (${bot}) is above the crowd price (${crowd}).`;
  }

  return `This bet buys NO at ${entry}. It pays out if the market resolves NO because the bot estimate for YES (${bot}) is below the crowd price (${crowd}).`;
}

function buildOpenBetExplanation(row: OpenPosition): string {
  const summary = buildOpenBetSummary(row);
  if (!row.reasoning) return summary;
  return `${summary}\n\nBecause ${row.reasoning}`;
}

function toggleSort<T extends string>(
  current: SortState<T>,
  key: T,
  defaults: Record<T, Direction>,
): SortState<T> {
  if (current.key === key) {
    return {
      key,
      direction: current.direction === "asc" ? "desc" : "asc",
    };
  }
  return { key, direction: defaults[key] };
}

function compareText(
  a: string | null | undefined,
  b: string | null | undefined,
  direction: Direction,
) {
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  return direction === "asc" ? a.localeCompare(b) : b.localeCompare(a);
}

function compareNumber(
  a: number | null | undefined,
  b: number | null | undefined,
  direction: Direction,
) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return direction === "asc" ? a - b : b - a;
}

function parseTime(iso: string | null | undefined) {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? null : ms;
}

function compareTime(
  a: string | null | undefined,
  b: string | null | undefined,
  direction: Direction,
) {
  return compareNumber(parseTime(a), parseTime(b), direction);
}

function sortOpenRows(rows: OpenPosition[], sort: SortState<OpenSortKey>) {
  return [...rows].sort((a, b) => {
    let result = 0;
    switch (sort.key) {
      case "question":
        result = compareText(a.question, b.question, sort.direction);
        break;
      case "side":
        result = compareText(a.side, b.side, sort.direction);
        break;
      case "cost":
        result = compareNumber(a.cost_usd, b.cost_usd, sort.direction);
        break;
      case "entry":
        result = compareNumber(a.entry_price, b.entry_price, sort.direction);
        break;
      case "bot":
        result = compareNumber(a.claude_probability, b.claude_probability, sort.direction);
        break;
      case "ev":
        result = compareNumber(a.ev_bps, b.ev_bps, sort.direction);
        break;
      case "confidence":
        result = compareNumber(a.confidence, b.confidence, sort.direction);
        break;
      case "resolution":
        result = compareTime(a.expected_resolution_at, b.expected_resolution_at, sort.direction);
        break;
    }
    if (result !== 0) return result;
    return b.id - a.id;
  });
}

function sortSettledRows(rows: SettledPosition[], sort: SortState<SettledSortKey>) {
  return [...rows].sort((a, b) => {
    let result = 0;
    switch (sort.key) {
      case "question":
        result = compareText(a.question, b.question, sort.direction);
        break;
      case "side":
        result = compareText(a.side, b.side, sort.direction);
        break;
      case "outcome":
        result = compareText(a.settlement_outcome, b.settlement_outcome, sort.direction);
        break;
      case "pnl":
        result = compareNumber(a.realized_pnl_usd, b.realized_pnl_usd, sort.direction);
        break;
      case "entry":
        result = compareNumber(a.entry_price, b.entry_price, sort.direction);
        break;
      case "settle":
        result = compareNumber(a.settlement_price, b.settlement_price, sort.direction);
        break;
      case "when":
        result = compareTime(a.settled_at, b.settled_at, sort.direction);
        break;
    }
    if (result !== 0) return result;
    return b.id - a.id;
  });
}

function OpenRows({
  rows,
  sort,
  setSort,
}: {
  rows: OpenPosition[] | undefined;
  sort: SortState<OpenSortKey>;
  setSort: Dispatch<SetStateAction<SortState<OpenSortKey>>>;
}) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const toggle = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const sortedRows = useMemo(
    () => (rows && rows.length > 0 ? sortOpenRows(rows, sort) : []),
    [rows, sort],
  );

  if (!rows || rows.length === 0) {
    return <Empty text="no open bets" />;
  }

  return (
    <table className="w-full text-xs">
      <thead className="sticky top-0 z-10 bg-[#050505] text-[#666] text-[10px] uppercase tracking-widest">
        <tr className="border-b border-[#0a0a0a]">
          <SortableTh label="market" sortKey="question" sort={sort} setSort={setSort} defaults={OPEN_DEFAULTS} />
          <SortableTh label="side" sortKey="side" sort={sort} setSort={setSort} defaults={OPEN_DEFAULTS} />
          <SortableTh label="stake" sortKey="cost" sort={sort} setSort={setSort} defaults={OPEN_DEFAULTS} right />
          <SortableTh
            label="entry price"
            sortKey="entry"
            sort={sort}
            setSort={setSort}
            defaults={OPEN_DEFAULTS}
            right
            className="hidden md:table-cell"
          />
          <SortableTh
            label="bot estimate"
            sortKey="bot"
            sort={sort}
            setSort={setSort}
            defaults={OPEN_DEFAULTS}
            right
            className="hidden md:table-cell"
          />
          <SortableTh
            label="EV"
            sortKey="ev"
            sort={sort}
            setSort={setSort}
            defaults={OPEN_DEFAULTS}
            right
            className="hidden lg:table-cell"
          />
          <SortableTh
            label="confidence"
            sortKey="confidence"
            sort={sort}
            setSort={setSort}
            defaults={OPEN_DEFAULTS}
            right
            className="hidden lg:table-cell"
          />
          <SortableTh label="resolves" sortKey="resolution" sort={sort} setSort={setSort} defaults={OPEN_DEFAULTS} right />
        </tr>
      </thead>
      <tbody>
        {sortedRows.map((r) => {
          const isOpen = expanded.has(r.id);
          return (
            <Fragment key={r.id}>
              <tr
                onClick={() => toggle(r.id)}
                className="border-b border-[#0a0a0a] hover:bg-[#0a0a0a]/50 cursor-pointer"
              >
                <td className="px-3 py-2">
                  <div className="flex items-start gap-1.5">
                    <span className="text-[#444] text-[10px] shrink-0 mt-0.5">{isOpen ? "▼" : "▶"}</span>
                    <span
                      className={`text-white ${isOpen ? "whitespace-normal" : "line-clamp-1"}`}
                      title={r.question}
                    >
                      {r.question}
                    </span>
                  </div>
                  <div className="text-[10px] text-[#444] ml-4">#{r.id} · {r.category ?? "other"}</div>
                </td>
                <td className="px-3 py-2"><Side side={r.side} /></td>
                <td className="px-3 py-2 text-right font-body text-white">{usd(r.cost_usd)}</td>
                <td className="px-3 py-2 text-right font-body text-white hidden md:table-cell">{sharePrice(r.entry_price)}</td>
                <td className="px-3 py-2 text-right font-body text-white hidden md:table-cell">{prob(r.claude_probability)}</td>
                <td className="px-3 py-2 text-right font-body text-white hidden lg:table-cell">
                  {r.ev_bps != null ? r.ev_bps.toFixed(0) : "—"}
                </td>
                <td className="px-3 py-2 text-right font-body text-white hidden lg:table-cell">
                  {r.confidence != null ? r.confidence.toFixed(2) : "—"}
                </td>
                <td className="px-3 py-2 text-right font-body text-[#a0a0a0]">{timeUntil(r.expected_resolution_at)}</td>
              </tr>
              {isOpen && (
                <tr className="border-b border-[#0a0a0a] bg-[#0a0a0a]/30">
                  <td colSpan={8} className="px-4 py-3">
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] mb-2">
                      <Detail label="shares" value={r.shares.toFixed(1)} />
                      <Detail label="stake" value={usd(r.cost_usd)} />
                      <Detail label="entry price" value={sharePrice(r.entry_price)} />
                      <Detail label="bot estimate" value={prob(r.claude_probability)} />
                      <Detail label="crowd price" value={prob(crowdPriceYes(r))} />
                      <Detail label="EV" value={r.ev_bps != null ? `${r.ev_bps.toFixed(0)} bps` : "—"} />
                      <Detail label="confidence" value={r.confidence?.toFixed(2) ?? "—"} />
                      <Detail label="opened" value={timeAgo(r.created_at)} />
                      <Detail label="resolves in" value={timeUntil(r.expected_resolution_at)} />
                    </div>
                    <div className="text-[10px] text-[#666] mb-1">why this bet</div>
                    <div className="text-xs text-[#ccc] whitespace-pre-wrap leading-relaxed">
                      {buildOpenBetExplanation(r)}
                    </div>
                    <div className="text-[10px] text-[#444] mt-3">
                      <a
                        href={polymarketUrl(r.slug, r.market_id, r.event_slug)}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[#00ffff] hover:text-[#00ffff]/70"
                        onClick={(e) => e.stopPropagation()}
                      >
                        open on Polymarket
                      </a>
                    </div>
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

function SettledRows({
  rows,
  sort,
  setSort,
}: {
  rows: SettledPosition[] | undefined;
  sort: SortState<SettledSortKey>;
  setSort: Dispatch<SetStateAction<SortState<SettledSortKey>>>;
}) {
  const sortedRows = useMemo(
    () => (rows && rows.length > 0 ? sortSettledRows(rows, sort) : []),
    [rows, sort],
  );

  if (!rows || rows.length === 0) {
    return <Empty text="no settled positions yet" />;
  }

  return (
    <table className="w-full text-xs">
      <thead className="sticky top-0 z-10 bg-[#050505] text-[#666] text-[10px] uppercase tracking-widest">
        <tr className="border-b border-[#0a0a0a]">
          <SortableTh label="market" sortKey="question" sort={sort} setSort={setSort} defaults={SETTLED_DEFAULTS} />
          <SortableTh label="side" sortKey="side" sort={sort} setSort={setSort} defaults={SETTLED_DEFAULTS} />
          <SortableTh label="outcome" sortKey="outcome" sort={sort} setSort={setSort} defaults={SETTLED_DEFAULTS} />
          <SortableTh label="P&L" sortKey="pnl" sort={sort} setSort={setSort} defaults={SETTLED_DEFAULTS} right />
          <SortableTh
            label="entry"
            sortKey="entry"
            sort={sort}
            setSort={setSort}
            defaults={SETTLED_DEFAULTS}
            right
            className="hidden md:table-cell"
          />
          <SortableTh
            label="settle"
            sortKey="settle"
            sort={sort}
            setSort={setSort}
            defaults={SETTLED_DEFAULTS}
            right
            className="hidden md:table-cell"
          />
          <SortableTh label="when" sortKey="when" sort={sort} setSort={setSort} defaults={SETTLED_DEFAULTS} right />
        </tr>
      </thead>
      <tbody>
        {sortedRows.map((r) => (
          <tr key={r.id} className="border-b border-[#0a0a0a] hover:bg-[#0a0a0a]/50">
            <td className="px-3 py-2">
              <span className="text-white line-clamp-2" title={r.question}>
                {r.question}
              </span>
              <div className="text-[10px] text-[#444]">#{r.id} · {r.category ?? "other"}</div>
            </td>
            <td className="px-3 py-2"><Side side={r.side} /></td>
            <td className={`px-3 py-2 font-body ${sideColorClass(r.settlement_outcome)}`}>
              {r.settlement_outcome ?? "—"}
            </td>
            <td className={`px-3 py-2 text-right font-body ${pnlColorClass(r.realized_pnl_usd)}`}>
              {usd(r.realized_pnl_usd, { sign: true, clampZero: true })}
            </td>
            <td className="px-3 py-2 text-right font-body text-white hidden md:table-cell">{sharePrice(r.entry_price)}</td>
            <td className="px-3 py-2 text-right font-body text-white hidden md:table-cell">{sharePrice(r.settlement_price)}</td>
            <td className="px-3 py-2 text-right font-body text-[#a0a0a0]">{timeAgo(r.settled_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SortableTh<T extends string>({
  label,
  sortKey,
  sort,
  setSort,
  defaults,
  right,
  className,
}: {
  label: string;
  sortKey: T;
  sort: SortState<T>;
  setSort: Dispatch<SetStateAction<SortState<T>>>;
  defaults: Record<T, Direction>;
  right?: boolean;
  className?: string;
}) {
  const active = sort.key === sortKey;
  const indicator = active ? (sort.direction === "asc" ? "↑" : "↓") : "↕";

  return (
    <Th right={right} className={className}>
      <button
        type="button"
        onClick={() => setSort((current) => toggleSort(current, sortKey, defaults))}
        className={`inline-flex w-full items-center gap-1 transition hover:text-[#ccc] ${
          right ? "justify-end" : "justify-start"
        }`}
        aria-label={`Sort by ${label}`}
      >
        <span>{label}</span>
        <span className={active ? "text-[#00ffff]" : "text-[#444]"}>{indicator}</span>
      </button>
    </Th>
  );
}
