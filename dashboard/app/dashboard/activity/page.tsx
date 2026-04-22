"use client";

import { useState } from "react";
import "../../styles/content.css";

type Kind = "execute" | "pass" | "update" | "resolve" | "scan" | "risk";

type Event = {
  date: string;
  time: string;
  kind: Kind;
  title: string;
  meta: string;
  detail?: string;
};

const ACTIVITY: Event[] = [
  { date: "Today", time: "14:02:17", kind: "execute", title: "Opened YES position · Fed rate cut Dec", meta: "$420 · p_win 0.78 · conf 0.81", detail: "Forecast YES at p_win 0.78 (above 0.65 floor). Direction agrees with market (both sides of 0.50 match). Expected return +14.2% after costs (above 5% floor). Confidence 0.81 → full stake." },
  { date: "Today", time: "13:48:42", kind: "pass", title: "Passed on BTC > $115k · p_win below 0.65 floor", meta: "p_win 0.58 · min 0.65", detail: "Forecast NO with p_win 0.58 on the chosen side. Below 0.65 minimum p_win — Gate 2 failed. Skipped." },
  { date: "Today", time: "13:31:09", kind: "update", title: "Updated forecast · GPT-5 Q1 → 0.72", meta: "was 0.68", detail: "New research added to memory: OpenAI dev-day announcement pushed forecast up 4 points." },
  { date: "Today", time: "13:05:50", kind: "resolve", title: "Resolved WIN · CPI above 3.2%", meta: "+$142.80", detail: "Delfi held YES at entry 49¢. Settled 100¢. Realized P&L +$142.80." },
  { date: "Today", time: "12:41:22", kind: "scan", title: "Scanned 384 active markets", meta: "12 shortlisted" },
  { date: "Today", time: "11:58:03", kind: "execute", title: "Opened NO position · BTC $120k", meta: "$260 · p_win 0.74 · conf 0.72" },
  { date: "Today", time: "11:11:20", kind: "pass", title: "Passed on SP500 Friday green · expected return too thin", meta: "exp. return +0.8% · min 5%", detail: "Forecast YES at p_win 0.67 (clears 0.65 floor). Direction agrees. But expected return is only +0.8% after costs — below 5% minimum. Gate 3 failed." },
  { date: "Today", time: "10:42:51", kind: "pass", title: "Passed on Tennis match · direction disagrees with market", meta: "Delfi YES 0.46 · market YES 0.58", detail: "Delfi's forecast puts YES below 0.50; market puts it above. Sides disagree — Gate 1 failed. Skipped." },
  { date: "Today", time: "11:22:17", kind: "pass", title: "Passed on Election odds shift · correlated", meta: "event risk" },
  { date: "Yesterday", time: "22:14:08", kind: "risk", title: "Streak cooldown cleared", meta: "2 winning trades in a row", detail: "Position sizes returning to baseline after streak rule." },
  { date: "Yesterday", time: "18:40:33", kind: "resolve", title: "Resolved LOSS · NFL Thursday moneyline", meta: "-$18.40", detail: "Delfi held YES at 0.58. Settled NO. Realized P&L -$18.40. Lesson: single-game sports have high idiosyncratic variance; sizing already reflects that." },
  { date: "Yesterday", time: "16:12:09", kind: "execute", title: "Opened YES position · GPT-5 Q1", meta: "$180 · p_win 0.72 · conf 0.70" },
  { date: "Yesterday", time: "14:55:41", kind: "update", title: "Calibration pass complete", meta: "50 settled trades", detail: "Suggested a 12% size reduction on geopolitical tags — awaiting your approval." },
];

const KIND_COPY: Record<Kind, { label: string }> = {
  execute: { label: "Trade" },
  pass: { label: "Pass" },
  update: { label: "Signal" },
  resolve: { label: "Resolution" },
  scan: { label: "Scan" },
  risk: { label: "Risk" },
};

type F = "all" | Kind;

export default function ActivityPage() {
  const [filter, setFilter] = useState<F>("all");
  const items = filter === "all" ? ACTIVITY : ACTIVITY.filter((a) => a.kind === filter);

  const byDate = items.reduce<Record<string, Event[]>>((acc, a) => {
    (acc[a.date] = acc[a.date] || []).push(a);
    return acc;
  }, {});

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Activity log</h1>
            <p className="page-sub">Every decision Delfi made, in order. Passes and scans included, not just trades.</p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm">Export log</button>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div className="page-toolbar-left">
          <button className={`chip ${filter === "all" ? "on" : ""}`} onClick={() => setFilter("all")}>All</button>
          <button className={`chip ${filter === "execute" ? "on" : ""}`} onClick={() => setFilter("execute")}>Trades</button>
          <button className={`chip ${filter === "resolve" ? "on" : ""}`} onClick={() => setFilter("resolve")}>Resolutions</button>
          <button className={`chip ${filter === "pass" ? "on" : ""}`} onClick={() => setFilter("pass")}>Passes</button>
          <button className={`chip ${filter === "update" ? "on" : ""}`} onClick={() => setFilter("update")}>Signal updates</button>
          <button className={`chip ${filter === "risk" ? "on" : ""}`} onClick={() => setFilter("risk")}>Risk events</button>
        </div>
      </div>

      {Object.entries(byDate).map(([date, events]) => (
        <div className="panel" key={date}>
          <div className="panel-head">
            <h2 className="panel-title">{date}</h2>
            <span className="panel-meta">{events.length} events</span>
          </div>
          {events.map((e, i) => (
            <div className="split-row" key={i}>
              <div className="split-body" style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
                <span className="mono" style={{ fontSize: 12, color: "var(--vellum-40)", minWidth: 80 }}>{e.time}</span>
                <span className="pill" style={{ minWidth: 68, textAlign: "center" }}>{KIND_COPY[e.kind].label}</span>
                <div style={{ flex: 1 }}>
                  <div className="split-title">{e.title}</div>
                  <div className="split-desc">{e.meta}</div>
                  {e.detail && <div className="split-desc" style={{ marginTop: 6, color: "var(--vellum-60)" }}>{e.detail}</div>}
                </div>
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
