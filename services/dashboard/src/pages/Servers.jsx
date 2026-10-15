import React, { useState } from "react";
import { api, fmtAgo } from "../api.js";
import { useMe } from "../App.jsx";
import { Lamp, Modal, useLoad } from "../components.jsx";

const blank = { name: "", slug: "", url: "", auth_type: "none", header_name: "", credential: "", enabled: true };

function ServerForm({ server, onDone, onClose }) {
  const editing = !!server;
  const [f, setF] = useState(editing ? { ...blank, name: server.name, url: server.url, auth_type: server.auth_type, header_name: server.header_name || "", enabled: server.enabled } : blank);
  const [clear, setClear] = useState(false);
  const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    const body = { name: f.name, url: f.url, auth_type: f.auth_type, header_name: f.auth_type === "header" ? f.header_name : null, enabled: f.enabled };
    if (f.auth_type === "none") body.credential = ""; else if (clear) body.credential = ""; else if (f.credential) body.credential = f.credential;
    try {
      if (editing) await api(`/servers/${server.id}`, { method: "PATCH", body });
      else await api("/servers", { method: "POST", body: { ...body, slug: f.slug || undefined } });
      onDone();
    } catch (ex) { setErr(ex.message); setBusy(false); }
  };
  return (
    <Modal title={editing ? `Edit ${server.name}` : "Add MCP server"} onClose={onClose}>
      <form onSubmit={submit} className="form">
        <label>Name<input value={f.name} onChange={set("name")} required autoFocus /></label>
        {!editing && <label>Short name for tools<input value={f.slug} onChange={set("slug")} placeholder="auto from name" pattern="[a-z0-9][a-z0-9\-]{0,22}[a-z0-9]?" />
          <span className="hint">Tools appear as <code>{f.slug || "name"}__toolname</code>. Cannot be changed later.</span></label>}
        <label>Server URL<input value={f.url} onChange={set("url")} placeholder="https://host/mcp" required />
          <span className="hint">Streamable HTTP endpoint. Internal and metadata addresses are blocked unless the operator allows them.</span></label>
        <label>Credential type
          <select value={f.auth_type} onChange={set("auth_type")}><option value="none">None</option><option value="bearer">Bearer token</option><option value="header">Custom header</option></select></label>
        {f.auth_type === "header" && <label>Header name<input value={f.header_name} onChange={set("header_name")} placeholder="X-Api-Key" required /></label>}
        {f.auth_type !== "none" && (<label>Credential
          <input type="password" autoComplete="off" value={f.credential} onChange={set("credential")} disabled={clear}
            placeholder={editing && server.has_credential ? "Stored. Leave blank to keep it." : ""} />
          <span className="hint">Stored encrypted. It is never shown again or sent to browsers or MCP clients.</span>
          {editing && server.has_credential && <span className="check"><input type="checkbox" checked={clear} onChange={(e) => setClear(e.target.checked)} /> Remove stored credential</span>}
        </label>)}
        <label className="check"><input type="checkbox" checked={f.enabled} onChange={set("enabled")} /> Enabled</label>
        {err && <div className="error" role="alert">{err}</div>}
        <div className="actions"><button type="button" className="ghost" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>{editing ? "Save changes" : "Add server"}</button></div>
      </form>
    </Modal>
  );
}

function Policies({ server, onClose }) {
  const { data, loading } = useLoad(() => api(`/servers/${server.id}/policies`), [server.id]);
  const [sel, setSel] = useState(null); const [err, setErr] = useState(""); const [saved, setSaved] = useState(false);
  const restricted = sel ?? (data?.restricted_tools || {});
  const toggle = (t) => { const n = { ...restricted }; if (n[t]) delete n[t]; else n[t] = "admin"; setSel(n); setSaved(false); };
  const save = async () => { try { await api(`/servers/${server.id}/policies`, { method: "PUT", body: { restricted_tools: restricted } }); setSaved(true); } catch (e) { setErr(e.message); } };
  return (
    <Modal title={`Who can use ${server.name} tools`} onClose={onClose}>
      <p className="muted">Admins can always use every tool. Restricted tools are hidden from members and rejected if called directly.</p>
      {loading ? <p>Loading…</p> : (
        <ul className="plain rows">
          {server.tools.map((t) => (
            <li key={t.name} className="row between">
              <div><code>{t.name}</code><div className="muted small">{t.description}</div></div>
              <label className="check"><input type="checkbox" checked={!!restricted[t.name]} onChange={() => toggle(t.name)} /> Admins only</label>
            </li>))}
        </ul>)}
      {err && <div className="error">{err}</div>}
      <div className="actions">{saved && <span className="ok">Saved</span>}<button className="primary" onClick={save}>Save permissions</button></div>
    </Modal>
  );
}

export default function Servers() {
  const me = useMe(); const admin = me.role === "admin";
  const { data, error, loading, reload } = useLoad(() => api("/servers"), [], 5000);
  const [modal, setModal] = useState(null); const [open, setOpen] = useState({});
  const act = async (fn) => { try { await fn(); reload(); } catch (e) { alert(e.message); } };
  const remove = (s) => confirm(`Remove ${s.name}? Its tools disappear for everyone.`) && act(() => api(`/servers/${s.id}`, { method: "DELETE" }));

  return (
    <>
      <header className="page-head">
        <div><h1>Servers</h1><p className="muted">Every enabled server is available to the whole team through one gateway URL.</p></div>
        {admin && <button className="primary" onClick={() => setModal({ type: "add" })}>Add server</button>}
      </header>
      {error && <div className="error">{error}</div>}
      {loading && !data && <p>Loading…</p>}
      {data && data.length === 0 && <p className="empty">No servers yet. {admin ? "Add one by URL to start exposing its tools." : "Ask an admin to connect one."}</p>}
      <ul className="plain board">
        {data?.map((s) => (
          <li key={s.id} className={`server ${s.status}`}>
            <div className="row between wrap">
              <div className="grow">
                <div className="row gap"><h2>{s.name}</h2><Lamp status={s.status} /></div>
                <div className="muted small">
                  <code>{s.slug}__*</code> · {s.tools.length} {s.tools.length === 1 ? "tool" : "tools"}
                  {s.latency_ms != null && s.status === "connected" ? ` · ${s.latency_ms} ms` : ""} · checked {s.checked_at ? fmtAgo(new Date(s.checked_at * 1000)) : "never"}
                </div>
                {admin && s.status === "unreachable" && s.error && <div className="why">Why: {s.error}</div>}
                {admin && <div className="muted small mono-url">{s.url}{s.has_credential ? " · credential stored" : ""}</div>}
              </div>
              <div className="row gap">
                {s.tools.length > 0 && <button className="ghost" onClick={() => setOpen({ ...open, [s.id]: !open[s.id] })}>{open[s.id] ? "Hide tools" : "Show tools"}</button>}
                {admin && <>
                  <button className="ghost" onClick={() => setModal({ type: "policy", s })} disabled={!s.tools.length}>Permissions</button>
                  <button className="ghost" onClick={() => act(() => api(`/servers/${s.id}`, { method: "PATCH", body: { enabled: !s.enabled } }))}>{s.enabled ? "Disable" : "Enable"}</button>
                  <button className="ghost" onClick={() => setModal({ type: "edit", s })}>Edit</button>
                  <button className="ghost danger" onClick={() => remove(s)}>Remove</button></>}
              </div>
            </div>
            {open[s.id] && <ul className="plain tools">{s.tools.map((t) => <li key={t.name}><code>{t.exposed_name}</code><span className="muted small">{t.description}</span></li>)}</ul>}
          </li>))}
      </ul>
      {modal?.type === "add" && <ServerForm onClose={() => setModal(null)} onDone={() => { setModal(null); reload(); }} />}
      {modal?.type === "edit" && <ServerForm server={modal.s} onClose={() => setModal(null)} onDone={() => { setModal(null); reload(); }} />}
      {modal?.type === "policy" && <Policies server={modal.s} onClose={() => { setModal(null); reload(); }} />}
    </>
  );
}
