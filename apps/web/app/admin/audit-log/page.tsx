"use client";

import { useEffect, useMemo, useState } from "react";
import { getJSON } from "@/lib/fetch-json";

type KindFilter = "all" | "config" | "event";

type AuditEntry = {
  kind:         "config" | "event";
  id:           string;
  timestamp:    string | null;
  user_id:      string;
  email:        string | null;
  event_type:   string | null;
  severity:     number | null;
  description:  string | null;
  source:       string | null;
  param_name:   string | null;
  old_value:    string | null;
  new_value:    string | null;
  reason:       string | null;
  outcome:      string | null;
};

type AuditPayload = {
  entries: AuditEntry[];
  limit:   number;
  offset:  number;
};

const PAGE_SIZE = 100;

function fmtDateTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function truncate(s: string | null, n: number): string {
  if (!s) return "-";
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "...";
}

function severityPill(sev: number | null): { label: string; klass: string } {
  if (sev === null || sev === undefined) return { label: "-", klass: "pill-skip" };
  if (sev >= 3) return { label: `sev ${sev}`, klass: "pill-lost" };
  if (sev === 2) return { label: `sev ${sev}`, klass: "pill-no" };
  return { label: `sev ${sev}`, klass: "pill-skip" };
}

export default function AdminAuditLogPage() {
  const [data, setData]       = useState<AuditPayload | null>(null);
  const [loaded, setLoaded]   = useState(false);
  const [error, setError]     = useState<string | null>(null);
  const [kind, setKind]       = useState<KindFilter>("all");
  const [q, setQ]             = useState("");
  const [qDebounced, setQD]   = useState("");
  const [userFilter, setUser] = useState("");
  const [userDeb, setUserDeb] = useState("");
  const [offset, setOffset]   = useState(0);

  useEffect(() => {
    const t = setTimeout(() => setQD(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    const t = setTimeout(() => setUserDeb(userFilter.trim()), 300);
    return () => clearTimeout(t);
  }, [userFilter]);

  useEffect(() => {
    setOffset(0);
  }, [kind, qDebounced, userDeb]);

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams();
    if (kind !== "all") params.set("kind", kind);
    if (qDebounced) params.set("q", qDebounced);
    if (userDeb) params.set("user_id", userDeb);
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(offset));

    const load = async () => {
      setError(null);
      try {
        const res = await getJSON<AuditPayload>(`/api/admin/audit-log?${params.toString()}`);
        if (cancelled) return;
        setData(res);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load audit log");
      } finally {
        if (!cancelled) setLoaded(true);
      }
    };
    load();
    return () => { cancelled = true; };
  }, [kind, qDebounced, userDeb, offset]);

  const entries = data?.entries ?? [];

  const summary = useMemo(() => {
    if (!data) return "";
    if (entries.length === 0) return "0 entries";
    return `${entries.length} entries`;
  }, [data, entries]);

  const canPrev = offset > 0;
  const canNext = entries.length >= PAGE_SIZE;

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Audit log</h1>
            <p className="page-sub">Config changes and system events across every tenant. Cross-user search and filter.</p>
          </div>
        </div>
      </div>

      <div className="page-toolbar" style={{ flexWrap: "wrap", gap: 12 }}>
        <div style={{ flex: "1 1 280px", maxWidth: 360 }}>
          <input
            className="ob-input"
            placeholder="Search description, param, reason"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>

        <div style={{ flex: "1 1 240px", maxWidth: 320 }}>
          <input
            className="ob-input"
            placeholder="Filter by user id"
            value={userFilter}
            onChange={(e) => setUser(e.target.value)}
          />
        </div>

        <div className="tab-bar" style={{ padding: 0, margin: 0 }}>
          {(["all", "config", "event"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={`tab ${kind === k ? "on" : ""}`}
              style={{ textTransform: "capitalize" }}
            >
              {k === "all" ? "All" : k}
            </button>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">{loaded ? summary : "Loading audit log..."}</h2>
        </div>

        {error ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">{error}</div></div></div>
        ) : !loaded ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">Loading...</div></div></div>
        ) : entries.length === 0 ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">No matching entries.</div></div></div>
        ) : (
          <table className="table-simple">
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>User</th>
                <th>What</th>
                <th>Details</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => {
                const userLabel = e.email || e.user_id.slice(0, 8);
                const isConfig = e.kind === "config";
                const what = isConfig
                  ? `${e.param_name ?? "-"}: ${truncate(e.old_value, 24)} -> ${truncate(e.new_value, 24)}`
                  : (e.event_type ?? "-");
                const details = isConfig
                  ? (e.reason ?? "-")
                  : (e.description ?? "-");
                const sevPill = !isConfig ? severityPill(e.severity) : null;
                return (
                  <tr key={e.id}>
                    <td className="mono" style={{ color: "var(--vellum-60)", whiteSpace: "nowrap" }}>
                      {fmtDateTime(e.timestamp)}
                    </td>
                    <td>
                      <span className={`pill ${isConfig ? "pill-open" : "pill-no"}`}>
                        {e.kind}
                      </span>
                      {sevPill ? (
                        <span className={`pill ${sevPill.klass}`} style={{ marginLeft: 6 }}>
                          {sevPill.label}
                        </span>
                      ) : null}
                    </td>
                    <td>
                      <div style={{ fontSize: 13 }}>{truncate(userLabel, 28)}</div>
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>{what}</td>
                    <td style={{ maxWidth: 480 }}>
                      <div className="split-desc">{truncate(details, 160)}</div>
                      {isConfig && e.outcome ? (
                        <div className="split-desc" style={{ fontSize: 11 }}>
                          outcome: {e.outcome}
                        </div>
                      ) : null}
                    </td>
                    <td className="mono" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
                      {e.source ?? "-"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        {loaded && entries.length > 0 ? (
          <div className="split-row" style={{ justifyContent: "space-between" }}>
            <div className="split-body">
              <div className="split-desc">Page {Math.floor(offset / PAGE_SIZE) + 1}</div>
            </div>
            <div className="split-right" style={{ display: "flex", gap: 8 }}>
              <button
                className="btn-sm"
                disabled={!canPrev}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Prev
              </button>
              <button
                className="btn-sm"
                disabled={!canNext}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}
