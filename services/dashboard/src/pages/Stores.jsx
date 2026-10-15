import React, { useState } from "react";
import { api, fmtAgo, fmtTime } from "../api.js";
import { Modal, useLoad } from "../components.jsx";

const LABEL = { provisioning: "Provisioning", ready: "Ready", failed: "Failed", deleting: "Deleting" };

function StoreLamp({ status }) {
  return <span className={`lamp store-${status}`}><i aria-hidden />{LABEL[status] || status}</span>;
}

function CreateStore({ onClose, onDone }) {
  const [name, setName] = useState(""); const [slug, setSlug] = useState("");
  const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  const auto = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 20).replace(/-+$/, "");
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    try { await api("/stores", { method: "POST", body: { name, slug: slug || undefined } }); onDone(); }
    catch (x) { setErr(x.message); setBusy(false); }
  };
  return (
    <Modal title="Create a store" onClose={onClose}>
      <form onSubmit={submit} className="form">
        <label>Store name<input value={name} onChange={(e) => setName(e.target.value)} required autoFocus placeholder="Chai Corner" /></label>
        <label>Short name<input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder={auto || "auto from name"} pattern="[a-z0-9][a-z0-9\-]{0,18}[a-z0-9]?" />
          <span className="hint">Used for the storefront address and its tools: <code>{(slug || auto || "name")}__list_orders</code>. Cannot be changed later.</span></label>
        <p className="muted small">The store gets its own namespace, database, volumes, secrets and address. Its WooCommerce MCP server is connected to the gateway automatically once it is ready (usually 2 to 4 minutes).</p>
        {err && <div className="error" role="alert">{err}</div>}
        <div className="actions"><button type="button" className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy}>{busy ? "Creating…" : "Create store"}</button></div>
      </form>
    </Modal>
  );
}

export default function Stores() {
  const { data, error, reload } = useLoad(() => api("/stores"), [], 4000);
  const [open, setOpen] = useState(false); const [msg, setMsg] = useState("");
  const act = async (fn) => { setMsg(""); try { await fn(); reload(); } catch (e) { setMsg(e.message); } };
  const remove = (s) => confirm(`Delete ${s.name}? Its namespace, database, volumes, secrets and tools are removed for everyone. This cannot be undone.`)
    && act(() => api(`/stores/${s.id}`, { method: "DELETE" }));
  const stores = data?.stores || [];

  return (
    <>
      <header className="page-head">
        <div><h1>Stores</h1>
          <p className="muted">Each store runs in its own namespace with its own database and MCP server. Its tools appear in the gateway as <code>&lt;short name&gt;__tool</code>.</p></div>
        {data?.enabled && <button className="primary" onClick={() => setOpen(true)}>Create store</button>}
      </header>
      {(error || msg) && <div className="error" role="alert">{error || msg}</div>}
      {data && !data.enabled && <p className="empty">Store provisioning is turned off for this installation (chart value <code>stores.enabled</code>).</p>}
      {data?.enabled && <p className="muted small">Up to {data.max_per_user} stores per person · a store that is not ready within {Math.round(data.timeout_seconds / 60)} minutes is marked Failed with the reason.</p>}
      {data?.enabled && stores.length === 0 && <p className="empty">No stores yet. Create one to get a storefront and its tools in a few minutes.</p>}
      <ul className="plain board">
        {stores.map((s) => (
          <li key={s.id} className={`store ${s.status}`}>
            <div className="row between wrap">
              <div className="grow">
                <div className="row gap"><h2>{s.name}</h2><StoreLamp status={s.status} /></div>
                <div className="muted small">
                  <code>{s.tool_prefix}*</code> · namespace <code>{s.namespace}</code> · created {fmtAgo(s.created_at)} by {s.owner}
                  {s.ready_at ? ` · ready ${fmtTime(s.ready_at)}` : ""}
                </div>
                {s.status === "ready"
                  ? <div className="mono-url"><a href={s.url} target="_blank" rel="noreferrer">{s.url}</a></div>
                  : <div className="muted small mono-url">{s.url}</div>}
                {s.reason && s.status !== "ready" && <div className={s.status === "failed" ? "why" : "muted small"}>{s.status === "failed" ? "Why: " : ""}{s.reason}</div>}
              </div>
              {s.can_manage && s.status !== "deleting" && (
                <div className="row gap">
                  {s.status === "failed" && <button className="ghost" onClick={() => act(() => api(`/stores/${s.id}/retry`, { method: "POST" }))}>Retry</button>}
                  <button className="ghost danger" onClick={() => remove(s)}>Delete</button>
                </div>)}
            </div>
          </li>))}
      </ul>
      {open && <CreateStore onClose={() => setOpen(false)} onDone={() => { setOpen(false); reload(); }} />}
    </>
  );
}
