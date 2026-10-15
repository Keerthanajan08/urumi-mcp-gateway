import React, { useState } from "react";
import { api } from "../api.js";
import { useLoad } from "../components.jsx";

const HOURS = [1, 24, 168];

export default function Stats() {
  const [hours, setHours] = useState(24);
  const { data, error } = useLoad(() => api(`/stats?hours=${hours}`), [hours], 10000);
  return (
    <>
      <header className="page-head"><div><h1>Health &amp; metrics</h1>
        <p className="muted">Calls, error rate and latency per server, and why an upstream is unreachable. Raw Prometheus metrics are at <code>/metrics</code> inside the cluster.</p></div></header>
      <div className="row gap inline-form">
        <select value={hours} onChange={(e) => setHours(Number(e.target.value))} aria-label="Time range">
          {HOURS.map((h) => <option key={h} value={h}>{h === 1 ? "Last hour" : h === 24 ? "Last 24 hours" : "Last 7 days"}</option>)}
        </select>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Server</th><th>Status</th><th className="num">Calls</th><th className="num">Error rate</th><th className="num">p50</th><th className="num">p95</th><th>Outcomes</th></tr></thead>
          <tbody>
            {data?.servers.length === 0 && <tr><td colSpan="7" className="empty">No calls in this period.</td></tr>}
            {data?.servers.map((s) => (
              <tr key={s.server}>
                <td><code>{s.server}</code></td>
                <td>{s.status || "—"}{s.unreachable_reason && <div className="muted small">{s.unreachable_reason}</div>}</td>
                <td className="num">{s.calls}</td><td className="num">{(s.error_rate * 100).toFixed(1)}%</td>
                <td className="num">{s.p50_ms} ms</td><td className="num">{s.p95_ms} ms</td>
                <td className="muted small">{Object.entries(s.outcomes).map(([k, v]) => `${k.replace("_", " ")} ${v}`).join(" · ")}</td>
              </tr>))}
          </tbody>
        </table>
      </div>
    </>
  );
}
