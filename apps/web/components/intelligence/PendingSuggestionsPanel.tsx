"use client";

/**
 * PendingSuggestionsPanel — learning-cadence proposals awaiting the user's
 * decision.
 *
 * The bot's learning cadence proposes config changes every 50 settled
 * trades (backed by backtester evidence). Nothing takes effect until the
 * user clicks Apply. Both scalar proposals (min_p_win → 0.70) and
 * list-append proposals (add "politics" to archetype_skip_list) are
 * rendered here. Dispatch happens server-side from the row's metadata.
 */

import { useCallback, useEffect, useState } from "react";

type Suggestion = {
  id: number;
  created_at: string | null;
  param_name: string;
  current_value: number | null;
  proposed_value: number | null;
  evidence: string;
  backtest_delta: number | null;
  backtest_trades: number | null;
  status: string;
  settled_count: number | null;
  metadata: {
    operation?: string;
    target_field?: string;
    items?: string[];
    field?: string;
    value?: number;
  } | null;
};

type SuggestionsResponse = {
  user_id: string;
  suggestions: Suggestion[];
};

function isListAppend(s: Suggestion): boolean {
  return s.metadata?.operation === "list_append";
}

function formatPct(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(2)}%`;
}

function formatList(items: readonly string[]): string {
  return items.length === 0 ? "[]" : `[${items.join(", ")}]`;
}

export function PendingSuggestionsPanel() {
  const [rows, setRows] = useState<Suggestion[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [busy, setBusy] = useState<number | null>(null);
  const [currentLists, setCurrentLists] = useState<Record<string, string[]>>({});

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [sRes, cRes] = await Promise.all([
        fetch("/api/suggestions", { cache: "no-store" }),
        fetch("/api/user-config", { cache: "no-store" }),
      ]);
      const sBody: SuggestionsResponse = await sRes.json();
      setRows(sBody.suggestions ?? []);
      if (cRes.ok) {
        const cBody = await cRes.json();
        const cfg = cBody?.config ?? {};
        setCurrentLists({
          archetype_skip_list: Array.isArray(cfg.archetype_skip_list)
            ? cfg.archetype_skip_list
            : [],
          ev_bucket_skip_list: Array.isArray(cfg.ev_bucket_skip_list)
            ? cfg.ev_bucket_skip_list
            : [],
        });
      }
    } catch {
      // Swallow — panel shows "no suggestions" fallback.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const act = useCallback(
    async (id: number, kind: "apply" | "skip" | "snooze") => {
      setBusy(id);
      try {
        const res = await fetch(`/api/suggestions/${id}/${kind}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          console.warn(`suggestion ${kind} failed`, body);
        }
      } finally {
        setBusy(null);
        refresh();
      }
    },
    [refresh],
  );

  const pending = rows.filter(
    (r) => r.status === "pending" || r.status === "snoozed",
  );

  return (
    <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#1a1a1a]">
        <h3 className="text-xs uppercase tracking-widest text-[#a0a0a0]">
          Pending Proposals
        </h3>
        <span className="text-[10px] text-[#444]">
          <span className="text-accent font-body">{pending.length}</span> awaiting review
        </span>
      </div>

      <div className="divide-y divide-[#1a1a1a]/50">
        {loading ? (
          <div className="px-4 py-6 text-xs text-[#666]">loading…</div>
        ) : pending.length === 0 ? (
          <div className="px-4 py-6 text-xs text-[#666]">
            No pending proposals. The cadence runs every 50 settled trades.
          </div>
        ) : (
          pending.map((s) => (
            <SuggestionRow
              key={s.id}
              suggestion={s}
              currentList={
                isListAppend(s) && s.metadata?.target_field
                  ? currentLists[s.metadata.target_field] ?? []
                  : undefined
              }
              busy={busy === s.id}
              onApply={() => act(s.id, "apply")}
              onSkip={() => act(s.id, "skip")}
              onSnooze={() => act(s.id, "snooze")}
            />
          ))
        )}
      </div>
    </div>
  );
}

function SuggestionRow({
  suggestion,
  currentList,
  busy,
  onApply,
  onSkip,
  onSnooze,
}: {
  suggestion: Suggestion;
  currentList: string[] | undefined;
  busy: boolean;
  onApply: () => void;
  onSkip: () => void;
  onSnooze: () => void;
}) {
  const meta = suggestion.metadata ?? {};
  const isList = isListAppend(suggestion);

  const label = isList
    ? meta.target_field ?? suggestion.param_name
    : suggestion.param_name;

  let currentDisplay: string;
  let proposedDisplay: string;

  if (isList) {
    const items = meta.items ?? [];
    const curr = currentList ?? [];
    const next = [...curr];
    for (const x of items) if (!next.includes(x)) next.push(x);
    currentDisplay = formatList(curr);
    proposedDisplay = formatList(next);
  } else {
    currentDisplay = formatPct(suggestion.current_value);
    proposedDisplay = formatPct(suggestion.proposed_value);
  }

  const delta = suggestion.backtest_delta;
  const deltaText =
    delta == null
      ? null
      : `${delta >= 0 ? "+" : ""}${(delta * 100).toFixed(2)}% backtest ROI`;

  return (
    <div className="px-4 py-3 text-xs">
      <div className="flex items-center justify-between">
        <div className="font-body text-[#d0d0d0]">{label}</div>
        {deltaText && (
          <div
            className={
              delta != null && delta >= 0 ? "text-green-400" : "text-red-400"
            }
          >
            {deltaText}
          </div>
        )}
      </div>

      <div className="mt-1 text-[#a0a0a0]">
        <span className="text-[#666]">{currentDisplay}</span>
        <span className="mx-2 text-[#444]">→</span>
        <span className="text-accent">{proposedDisplay}</span>
      </div>

      {suggestion.evidence && (
        <p className="mt-2 text-[11px] leading-relaxed text-[#888]">
          {suggestion.evidence}
        </p>
      )}

      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={onApply}
          disabled={busy}
          className="px-3 py-1 text-[10px] uppercase tracking-widest bg-accent/10 border border-accent/30 text-accent hover:bg-accent/20 disabled:opacity-50"
        >
          Apply
        </button>
        <button
          type="button"
          onClick={onSkip}
          disabled={busy}
          className="px-3 py-1 text-[10px] uppercase tracking-widest border border-[#333] text-[#888] hover:text-white disabled:opacity-50"
        >
          Skip
        </button>
        <button
          type="button"
          onClick={onSnooze}
          disabled={busy}
          className="px-3 py-1 text-[10px] uppercase tracking-widest border border-[#333] text-[#888] hover:text-white disabled:opacity-50"
        >
          Snooze
        </button>
      </div>
    </div>
  );
}
