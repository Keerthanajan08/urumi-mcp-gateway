import React, { useState } from "react";
import { api, fmtAgo } from "../api.js";
import { Modal, useLoad } from "../components.jsx";

function Deploy({ onClose, onDone }) {
  const [f, setF] = useState({ name: "", image: "", port: 8000, path: "/mcp", env: "" });
  const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const submit = async (e) => {
    e.preventDefault(); setErr(""); setBusy(true);
    try {
      const env = {};
      f.env.split("\n").map((l) => l.trim()).filter(Boolean).forEach((l) => { const i = l.indexOf("="); if (i < 1) throw new Error(`Bad line: ${l}`); env[l.slice(0, i)] = l.slice(i + 1); });
      await api("/hosted", { method: "POST", body: { name: f.name, image: f.image, port: Number(f.port), path: f.path, env } });
      onDone();
    } catch (x) { setErr(x.message); setBusy(false); }
  };
  return (
    <Modal title="Deploy a hosted MCP server" onClose={onClose}>
      <form onSubmit={submit} className="form">
        <label>Name<input value={f.name} onChange={set("name")} required autoFocus /></label>
        <label>Container image<input value={f.image} onChange={set("image")} placeholder="ghcr.io/your-org/my-mcp:1.0" required /></label>
        <label>Port<input type="number" min="1024" max="65535" value={f.port} onChange={set("port")} /></label>
        <label>MCP path<input value={f.path} onChange={set("path")} /></label>
        <label>Environment (one NAME=value per line; stored in a Kubernetes Secret, never shown again)<textarea rows="4" value={f.env} onChange={set("env")} /></label>
        {err && <div className="error">{err}</div>}
        <button className="primary" disabled={busy}>{busy ? "Deploying…" : "Deploy"}</button>
      </form>
    </Modal>
  );
}

export default function Hosted() {
  const { data, error, reload } = useLoad(() => api("/hosted"), [], 10000);
  const [open, setOpen] = useState(false); const [msg, setMsg] = useState("");
  const remove = async (s) => {
    if (!confirm(`Remove ${s.name}? Its Deployment, Service, NetworkPolicy and Secret are deleted.`)) return;
    try { await api(`/servers/${s.server_id}`, { method: "DELETE" }); reload?.(); } catch (e) { setMsg(e.message); }
  };
  return (
    <>
      <header className="page-head"><div><h1>Hosted servers</h1>
        <p className="muted">Run an MCP server from a container image. It gets its own Secret, resource limits and a NetworkPolicy that admits only the gateway.</p></div>
        <button className="primary" onClick={() => setOpen(true)}>Deploy server</button></header>
      {(error || msg) && <div className="error">{error || msg}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Image</th><th>Port</th><th>By</th><th>Created</th><th /></tr></thead>
          <tbody>
            {data?.length === 0 && <tr><td colSpan="6" className="empty">None yet. If this stays empty after a deploy attempt, hosted servers may be disabled in the chart.</td></tr>}
            {data?.map((s) => (
              <tr key={s.server_id}><td>{s.name} <code>{s.slug}</code></td><td><code>{s.image}</code></td><td>{s.port}</td><td>{s.created_by}</td>
                <td>{fmtAgo(s.created_at)}</td><td><button className="ghost" onClick={() => remove(s)}>Remove</button></td></tr>))}
          </tbody>
        </table>
      </div>
      {open && <Deploy onClose={() => setOpen(false)} onDone={() => { setOpen(false); reload?.(); }} />}
    </>
  );
}
