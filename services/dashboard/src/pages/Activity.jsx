import React, { useState } from "react";
import { api, fmtTime } from "../api.js";
import { useMe } from "../App.jsx";
import { useLoad } from "../components.jsx";

const OUTCOMES = ["", "success", "error", "timeout", "unreachable", "denied", "not_found"];
const PAGE = 50;

export default function Activity() {
  const me = useMe(); const admin = me.role === "admin";
  const [outcome, setOutcome] = useState(""); const [server, setServer] = useState(""); const [page, setPage] = useState(0);
  const q = new URLSearchParams({ limit: PAGE, offset: page * PAGE, ...(outcome && { outcome }), ...(server && { server }) });
  const { data, error } = useLoad(() => api(`/audit?${q}`), [outcome, server, page], 5000);
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE)) : 1;

  return (
    <>
      <header className="page-head"><div><h1>Activity</h1><p className="muted">{admin ? "Every tool call, from every user." : "Your tool calls."} Updates every few seconds.</p></div></header>
      <div className="row gap inline-form">
        <select value={outcome} onChange={(e) => { setOutcome(e.target.value); setPage(0); }} aria-label="Outcome">
          {OUTCOMES.map((o) => <option key={o} value={o}>{o || "Any outcome"}</option>)}</select>
        <input value={server} onChange={(e) => { setServer(e.target.value); setPage(0); }} placeholder="Server short name" aria-label="Server" />
      </div>
      {error && <div className="error">{error}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>When</th><th>User</th><th>Server</th><th>Tool</th><th>Via</th><th className="num">Took</th><th>Result</th></tr></thead>
          <tbody>
            {data?.items.length === 0 && <tr><td colSpan="7" className="empty">No calls match.</td></tr>}
            {data?.items.map((r) => (
              <tr key={r.id}>
                <td>{fmtTime(r.ts)}</td><td>{r.user}</td><td><code>{r.server || "—"}</code></td><td><code>{r.tool || "—"}</code></td>
                <td>{r.source === "chat" ? "Chat" : "MCP client"}</td><td className="num">{r.duration_ms} ms</td>
                <td><span className={`out ${r.outcome}`} title={r.error || ""}>{r.outcome.replace("_", " ")}</span>{r.error && <div className="muted small">{r.error}</div>}</td>
              </tr>))}
          </tbody>
        </table>
      </div>
      <div className="row gap pager"><button className="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>Newer</button>
        <span className="muted">Page {page + 1} of {pages}{data ? ` · ${data.total} calls` : ""}</span>
        <button className="ghost" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Older</button></div>
    </>
  );
}
