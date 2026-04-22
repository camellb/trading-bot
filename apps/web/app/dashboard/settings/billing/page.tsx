"use client";

const INVOICES = [
  { date: "2026-04-01", amount: 49.0, status: "paid", number: "INV-2026-0412" },
  { date: "2026-03-01", amount: 49.0, status: "paid", number: "INV-2026-0311" },
  { date: "2026-02-01", amount: 49.0, status: "paid", number: "INV-2026-0210" },
  { date: "2026-01-01", amount: 49.0, status: "paid", number: "INV-2026-0109" },
];

export default function BillingPage() {
  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Plan</h2>
          <span className="panel-meta">Current subscription</span>
        </div>

        <div className="kv-grid">
          <div className="kv-label">Plan</div>
          <div className="kv-val">Delfi Pro — $49 per month</div>
          <div className="kv-label">Next charge</div>
          <div className="kv-val">May 1, 2026 · $49.00</div>
          <div className="kv-label">Performance fee</div>
          <div className="kv-val">10% of net profits · billed monthly</div>
          <div className="kv-label">Billing email</div>
          <div className="kv-val mono">alex@morgan.co</div>
        </div>

        <div style={{ marginTop: 20, display: "flex", gap: 12 }}>
          <button className="btn-sm">Change plan</button>
          <button className="btn-sm">Update payment method</button>
          <button className="btn-sm danger">Cancel subscription</button>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Payment method</h2>
          <span className="panel-meta">On file</span>
        </div>
        <div className="kv-grid">
          <div className="kv-label">Type</div>
          <div className="kv-val">Visa ending in 4242</div>
          <div className="kv-label">Expires</div>
          <div className="kv-val mono">08 / 2028</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Invoices</h2>
          <span className="panel-meta">Last 12 months</span>
        </div>
        <table className="table-simple">
          <thead>
            <tr>
              <th>Date</th>
              <th>Number</th>
              <th>Amount</th>
              <th>Status</th>
              <th style={{ textAlign: "right" }}></th>
            </tr>
          </thead>
          <tbody>
            {INVOICES.map((inv, i) => (
              <tr key={i}>
                <td className="mono">{inv.date}</td>
                <td className="mono">{inv.number}</td>
                <td className="mono">${inv.amount.toFixed(2)}</td>
                <td><span className="pill pill-won">Paid</span></td>
                <td style={{ textAlign: "right" }}><button className="btn-sm">Download PDF</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
