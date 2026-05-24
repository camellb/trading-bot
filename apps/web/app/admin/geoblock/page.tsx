"use client";

import { useEffect, useMemo, useState } from "react";

import { COUNTRIES, countryForCode } from "@/lib/geoblock/countries";

type Rule = {
  id: number;
  country_code: string;
  subdivision_code: string | null;
  reason: string | null;
  created_at: string;
  created_by: string | null;
};

function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function explainError(raw: string): string {
  if (raw === "already_blocked") return "That country/region is already on the list.";
  if (raw === "forbidden") return "Admin only.";
  if (raw === "not_authenticated") return "Sign in first.";
  if (raw === "invalid_json") return "Malformed request.";
  return raw;
}

export default function AdminGeoblockPage() {
  const [rules, setRules] = useState<Rule[] | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  const [country, setCountry] = useState<string>("");
  const [subdivision, setSubdivision] = useState<string>("");
  const [reason, setReason] = useState<string>("");

  const load = async () => {
    try {
      const r = await fetch("/api/admin/geoblock", { cache: "no-store" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setError(
          body?.error ? explainError(String(body.error)) : `HTTP ${r.status}`,
        );
        setRules([]);
        return;
      }
      const j = (await r.json()) as { rules: Rule[] };
      setRules(j.rules ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoaded(true);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const summary = useMemo(() => {
    if (!rules) return "";
    const total = rules.length;
    const countryWide = rules.filter((r) => !r.subdivision_code).length;
    const subs = total - countryWide;
    if (total === 0) return "No regions blocked. Delfi is fully open.";
    return `${total} rule${total === 1 ? "" : "s"} active. ${countryWide} country-wide, ${subs} subdivision${subs === 1 ? "" : "s"}.`;
  }, [rules]);

  const sortedRules = useMemo(() => {
    if (!rules) return [];
    return [...rules].sort((a, b) => {
      const nameA = countryForCode(a.country_code)?.name ?? a.country_code;
      const nameB = countryForCode(b.country_code)?.name ?? b.country_code;
      if (nameA !== nameB) return nameA.localeCompare(nameB);
      const subA = a.subdivision_code ?? "";
      const subB = b.subdivision_code ?? "";
      return subA.localeCompare(subB);
    });
  }, [rules]);

  const addRule = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!country) {
      setError("Pick a country first.");
      return;
    }
    setSaving(true);
    setSavedMsg(null);
    setError(null);
    try {
      const r = await fetch("/api/admin/geoblock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          country_code: country,
          subdivision_code: subdivision.trim() || undefined,
          reason: reason.trim() || undefined,
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setError(body?.error ? explainError(String(body.error)) : `HTTP ${r.status}`);
        return;
      }
      const label = countryForCode(country)?.name ?? country;
      setSavedMsg(
        subdivision.trim()
          ? `Added ${label} / ${subdivision.trim().toUpperCase()}`
          : `Added ${label}`,
      );
      setCountry("");
      setSubdivision("");
      setReason("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add");
    } finally {
      setSaving(false);
    }
  };

  const removeRule = async (rule: Rule) => {
    const label = countryForCode(rule.country_code)?.name ?? rule.country_code;
    const region = rule.subdivision_code
      ? `${label} / ${rule.subdivision_code}`
      : label;
    const ok = confirm(
      `Remove the block on ${region}? Users in that region will regain access on their next request.`,
    );
    if (!ok) return;
    setSaving(true);
    setSavedMsg(null);
    setError(null);
    try {
      const r = await fetch(`/api/admin/geoblock?id=${rule.id}`, {
        method: "DELETE",
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setError(body?.error ? explainError(String(body.error)) : `HTTP ${r.status}`);
        return;
      }
      setSavedMsg(`Removed ${region}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove");
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div>
          <h1 className="page-h1">Geoblock</h1>
          <p className="page-sub">
            Countries and regions where Delfi is not available. Changes take effect
            on the next request (up to 30s cache). Admins always bypass these rules.
          </p>
        </div>
      </div>

      {error && (
        <div className="panel">
          <div className="split-row">
            <div className="split-body">
              <div className="split-title">Error</div>
              <div className="split-desc">{error}</div>
            </div>
          </div>
        </div>
      )}

      {savedMsg && (
        <div className="panel">
          <div className="split-row">
            <div className="split-body">
              <div className="split-desc">{savedMsg}. Live - no deploy needed.</div>
            </div>
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Add a block</h2>
        </div>
        <form onSubmit={addRule}>
          <div
            className="split-row"
            style={{ alignItems: "flex-end", flexWrap: "wrap", gap: 12 }}
          >
            <div className="split-body" style={{ minWidth: 220 }}>
              <div className="split-title" style={{ marginBottom: 4 }}>Country</div>
              <select
                className="ob-input"
                value={country}
                onChange={(e) => setCountry(e.target.value)}
                required
              >
                <option value="">Select country</option>
                {COUNTRIES.map((c) => (
                  <option key={c.code} value={c.code}>
                    {c.flag} {c.name} ({c.code})
                  </option>
                ))}
              </select>
            </div>

            <div className="split-body" style={{ minWidth: 160 }}>
              <div className="split-title" style={{ marginBottom: 4 }}>
                Subdivision{" "}
                <span style={{ color: "var(--vellum-60)", fontWeight: "normal" }}>
                  (optional)
                </span>
              </div>
              <input
                className="ob-input"
                placeholder="e.g. ON, CA, NY"
                value={subdivision}
                onChange={(e) => setSubdivision(e.target.value)}
                maxLength={10}
              />
            </div>

            <div className="split-body" style={{ minWidth: 240, flex: "1 1 240px" }}>
              <div className="split-title" style={{ marginBottom: 4 }}>
                Reason{" "}
                <span style={{ color: "var(--vellum-60)", fontWeight: "normal" }}>
                  (optional)
                </span>
              </div>
              <input
                className="ob-input"
                placeholder="e.g. CFTC restrictions"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                maxLength={500}
              />
            </div>

            <div className="split-right">
              <button
                type="submit"
                className="btn-sm gold"
                disabled={saving || !country}
              >
                {saving ? "Saving..." : "Add block"}
              </button>
            </div>
          </div>
          <div className="split-row">
            <div className="split-body">
              <div className="split-desc">
                Leave subdivision empty to block the entire country. Use ISO 3166-2
                suffixes (e.g. <code>ON</code> for Ontario, <code>NY</code> for New
                York) to block a single region.
              </div>
            </div>
          </div>
        </form>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">{loaded ? summary : "Loading..."}</h2>
        </div>
        {!loaded ? null : sortedRules.length === 0 ? (
          <div className="split-row">
            <div className="split-body">
              <div className="split-desc">No rules. Every region is open.</div>
            </div>
          </div>
        ) : (
          <table className="table-simple">
            <thead>
              <tr>
                <th>Country</th>
                <th>Region</th>
                <th>Reason</th>
                <th>Added</th>
                <th style={{ width: 80 }}></th>
              </tr>
            </thead>
            <tbody>
              {sortedRules.map((rule) => {
                const c = countryForCode(rule.country_code);
                return (
                  <tr key={rule.id}>
                    <td>
                      <span style={{ marginRight: 6 }}>{c?.flag ?? ""}</span>
                      {c?.name ?? rule.country_code}{" "}
                      <span
                        className="mono"
                        style={{ color: "var(--vellum-60)", fontSize: 12 }}
                      >
                        {rule.country_code}
                      </span>
                    </td>
                    <td className="mono" style={{ fontSize: 13 }}>
                      {rule.subdivision_code ? (
                        rule.subdivision_code
                      ) : (
                        <span style={{ color: "var(--vellum-60)" }}>whole country</span>
                      )}
                    </td>
                    <td style={{ maxWidth: 360 }}>
                      <div className="split-desc">{rule.reason || "-"}</div>
                    </td>
                    <td
                      className="mono"
                      style={{
                        color: "var(--vellum-60)",
                        whiteSpace: "nowrap",
                        fontSize: 12,
                      }}
                    >
                      {fmtDate(rule.created_at)}
                    </td>
                    <td>
                      <button
                        className="btn-sm danger"
                        disabled={saving}
                        onClick={() => removeRule(rule)}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </main>
  );
}
