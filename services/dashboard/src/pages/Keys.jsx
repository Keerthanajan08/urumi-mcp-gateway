import React, { useState } from "react";
import { api, fmtAgo } from "../api.js";
import { CopyBox, useLoad } from "../components.jsx";

export default function Keys() {
  const { data, error, reload } = useLoad(() => api("/keys"), []);
  const [name, setName] = useState(""); const [fresh, setFresh] = useState(null); const [err, setErr] = useState("");
  const origin = window.location.origin;

  const create = async (e) => {
    e.preventDefault(); setErr("");
    try { setFresh(await api("/keys", { method: "POST", body: { name } })); setName(""); reload(); } catch (ex) { setErr(ex.message); }
  };
  const revoke = async (k) => { if (confirm(`Revoke ${k.name || k.prefix}? Clients using it stop working immediately.`)) { await api(`/keys/${k.id}`, { method: "DELETE" }); reload(); } };
  const key = fresh?.key || "<your-key>";

  return (
    <>
      <header className="page-head"><div><h1>My API keys</h1><p className="muted">One key gives an MCP client every tool you are allowed to use. You never need the upstream servers' credentials.</p></div></header>
      <form className="row gap inline-form" onSubmit={create}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. Cursor on my laptop" aria-label="Key name" />
        <button className="primary">Generate key</button>
      </form>
      {err && <div className="error">{err}</div>}
      {fresh && (
        <section className="callout">
          <h2>Copy your new key now</h2>
          <p className="muted">It is stored only as a hash, so it cannot be shown again.</p>
          <CopyBox value={fresh.key} />
        </section>)}
      <section className="connect">
        <h2>Connect a client</h2>
        <p className="muted">Gateway URL</p><CopyBox value={`${origin}/mcp`} />
        <p className="muted">Claude Code</p>
        <CopyBox value={`claude mcp add --transport http urumi ${origin}/mcp --header "Authorization: Bearer ${key}"`} />
        <p className="muted">Cursor (<code>~/.cursor/mcp.json</code>) or MCP Inspector</p>
        <pre className="snippet">{JSON.stringify({ mcpServers: { urumi: { url: `${origin}/mcp`, headers: { Authorization: `Bearer ${key}` } } } }, null, 2)}</pre>
        <p className="muted">No key at all: sign in with your dashboard email and password instead (OAuth). Add the server without a header and your client opens a browser login.</p>
        <CopyBox value={`claude mcp add --transport http urumi ${origin}/mcp`} />
      </section>
      <h2 className="section-h">Your keys</h2>
      {error && <div className="error">{error}</div>}
      <ul className="plain rows">
        {data?.length === 0 && <li className="empty">No keys yet.</li>}
        {data?.map((k) => (
          <li key={k.id} className="row between">
            <div><strong>{k.name || "Unnamed key"}</strong> <code>{k.prefix}…</code>
              <div className="muted small">Created {fmtAgo(k.created_at)} · last used {fmtAgo(k.last_used_at)}</div></div>
            {k.revoked_at ? <span className="muted">Revoked {fmtAgo(k.revoked_at)}</span> : <button className="ghost danger" onClick={() => revoke(k)}>Revoke</button>}
          </li>))}
      </ul>
    </>
  );
}
