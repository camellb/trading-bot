"use client";

import { useState } from "react";
import Link from "next/link";
import "../../styles/content.css";

const FAQ = [
  {
    q: "What's the difference between Simulation and Live?",
    a: "Simulation uses paper capital. Delfi evaluates real markets and runs its real decision logic, but positions are recorded in a paper book. Live uses real capital from your connected wallet. The risk controls run identically in both modes.",
  },
  {
    q: "How do I connect my Polymarket wallet?",
    a: "Go to Settings → Account and click Reconnect wallet. You'll be asked to sign a scoped trading delegation with your smart wallet. Delfi only receives permission to open and close positions, never to withdraw funds.",
  },
  {
    q: "Can I set my own risk limits?",
    a: "Yes. Every risk parameter is user-editable in Risk Controls: daily loss cap, weekly cap, drawdown halt, per-trade maximum, dry-powder reserve, and streak cooldown. Values are bounded within a safe range to prevent catastrophic configurations.",
  },
  {
    q: "Why did Delfi pass on a market I thought was a good bet?",
    a: "Every candidate trade has to clear three gates: (1) direction — Delfi's forecast and the market must both be on the same side of 0.50; (2) minimum p_win — Delfi's probability for the chosen side has to be at least 0.65 by default; (3) minimum expected return after costs — at least 5% by default. A miss on any one gate means Delfi skips. The Activity log shows the exact gate each pass failed on.",
  },
  {
    q: "What happens when a risk cap triggers?",
    a: "Delfi stops opening new positions. Open positions remain managed. You receive an email (if enabled) and the banner on your dashboard will indicate the halt. Trading resumes the following day, or after the relevant cooldown.",
  },
  {
    q: "Can I apply a suggested config change automatically?",
    a: "No. Every config change, including those suggested by Delfi's own calibration pass, requires your explicit approval. Suggestions include the supporting data and a backtest delta so you can decide with full information.",
  },
  {
    q: "How do I cancel my subscription?",
    a: "Settings → Billing → Cancel subscription. Cancellation takes effect at the end of the current billing period. Your data remains accessible during that time.",
  },
  {
    q: "Does Delfi give financial advice?",
    a: "No. Delfi is a tool. Probability estimates, risk configuration suggestions, and weekly reviews are informational. Every trade uses your capital and your configured risk envelope; you are responsible for how you use the product.",
  },
];

export default function SupportPage() {
  const [open, setOpen] = useState<number | null>(0);
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Support</h1>
            <p className="page-sub">Find an answer quickly, or send us a message. We reply within one business day.</p>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Frequently asked</h2>
          <span className="panel-meta">{FAQ.length} topics</span>
        </div>

        {FAQ.map((f, i) => (
          <div className="split-row" key={i} style={{ cursor: "pointer", flexDirection: "column", alignItems: "stretch" }} onClick={() => setOpen(open === i ? null : i)}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
              <div className="split-title">{f.q}</div>
              <div className="mono" style={{ color: "var(--vellum-40)", fontSize: 18 }}>{open === i ? "−" : "+"}</div>
            </div>
            {open === i && (
              <div className="split-desc" style={{ marginTop: 10, color: "var(--vellum-60)", lineHeight: 1.6 }}>
                {f.a}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Send us a message</h2>
          <span className="panel-meta">support@delfi.app</span>
        </div>

        <div className="form-row">
          <div className="form-field">
            <label>Subject</label>
            <input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="What's on your mind?" />
          </div>
          <div className="form-field">
            <label>Message</label>
            <textarea rows={6} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Tell us what's happening. Include specifics if you can." />
            <div className="form-hint">We'll reply to the email on your account within one business day.</div>
          </div>
          <div>
            <button className="btn-sm gold">Send message</button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Other resources</h2>
          <span className="panel-meta">Policies and docs</span>
        </div>
        <div className="kv-grid">
          <div className="kv-label">Terms of service</div>
          <div className="kv-val"><Link href="/legal/terms" className="mono" style={{ color: "var(--teal)" }}>/legal/terms</Link></div>
          <div className="kv-label">Privacy policy</div>
          <div className="kv-val"><Link href="/legal/privacy" className="mono" style={{ color: "var(--teal)" }}>/legal/privacy</Link></div>
          <div className="kv-label">Risk disclosure</div>
          <div className="kv-val"><Link href="/legal/risk" className="mono" style={{ color: "var(--teal)" }}>/legal/risk</Link></div>
        </div>
      </div>
    </div>
  );
}
