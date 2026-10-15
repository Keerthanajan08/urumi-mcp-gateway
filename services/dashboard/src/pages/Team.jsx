import React, { useState } from "react";
import { api, fmtAgo } from "../api.js";
import { useMe } from "../App.jsx";
import { CopyBox, useLoad } from "../components.jsx";

export default function Team() {
  const me = useMe();
  const users = useLoad(() => api("/users"), []);
  const keys = useLoad(() => api("/keys?all=true"), []);
  const [f, setF] = useState({ email: "", name: "", role: "member" });
  const [invite, setInvite] = useState(null); const [fresh, setFresh] = useState(null); const [err, setErr] = useState("");
  const reload = () => { users.reload(); keys.reload(); };

  const send = async (e) => {
    e.preventDefault(); setErr("");
    try { setInvite(await api("/users/invite", { method: "POST", body: f })); setF({ email: "", name: "", role: "member" }); reload(); }
    catch (ex) { setErr(ex.message); }
  };
  const patch = async (u, body) => { try { await api(`/users/${u.id}`, { method: "PATCH", body }); reload(); } catch (e) { alert(e.message); } };
  const issue = async (u) => { try { setFresh({ ...(await api("/keys", { method: "POST", body: { user_id: u.id, name: "issued by admin" } })), for: u.email }); reload(); } catch (e) { alert(e.message); } };
  const revoke = async (k) => { if (confirm(`Revoke ${k.prefix}… for ${k.user_email}? Their next call is rejected.`)) { await api(`/keys/${k.id}`, { method: "DELETE" }); reload(); } };

  return (
    <>
      <header className="page-head"><div><h1>Team</h1><p className="muted">Admins manage servers and members. Members use tools through their own key.</p></div></header>
      <form className="row gap wrap inline-form" onSubmit={send}>
        <input type="email" placeholder="Email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} required aria-label="Email" />
        <input placeholder="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} aria-label="Name" />
        <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })} aria-label="Role"><option value="member">Member</option><option value="admin">Admin</option></select>
        <button className="primary">Invite</button>
      </form>
      {err && <div className="error">{err}</div>}
      {invite && <section className="callout"><h2>Invite for {invite.user.email}</h2><p className="muted">Send this link. It works once.</p><CopyBox value={`${window.location.origin}${invite.invite_path}`} /></section>}
      {fresh && <section className="callout"><h2>New key for {fresh.for}</h2><p className="muted">Shown once. Hand it over securely.</p><CopyBox value={fresh.key} /></section>}

      <h2 className="section-h">Members</h2>
      <ul className="plain rows">
        {users.data?.map((u) => (
          <li key={u.id} className="row between wrap">
            <div><strong>{u.name || u.email}</strong> <span className="muted">{u.email}</span>
              <div className="muted small">{u.role}{u.pending_invite ? " · invite pending" : ""}{u.disabled ? " · disabled" : ""}</div></div>
            <div className="row gap">
              <button className="ghost" onClick={() => issue(u)} disabled={u.disabled}>Issue key</button>
              <button className="ghost" disabled={u.id === me.id} onClick={() => patch(u, { role: u.role === "admin" ? "member" : "admin" })}>Make {u.role === "admin" ? "member" : "admin"}</button>
              <button className="ghost danger" disabled={u.id === me.id} onClick={() => patch(u, { disabled: !u.disabled })}>{u.disabled ? "Enable" : "Disable"}</button>
            </div>
          </li>))}
      </ul>

      <h2 className="section-h">All keys</h2>
      <ul className="plain rows">
        {keys.data?.length === 0 && <li className="empty">No keys issued yet.</li>}
        {keys.data?.map((k) => (
          <li key={k.id} className="row between">
            <div><code>{k.prefix}…</code> <strong>{k.name || "Unnamed key"}</strong> <span className="muted">{k.user_email}</span>
              <div className="muted small">Created {fmtAgo(k.created_at)} · last used {fmtAgo(k.last_used_at)}</div></div>
            {k.revoked_at ? <span className="muted">Revoked</span> : <button className="ghost danger" onClick={() => revoke(k)}>Revoke</button>}
          </li>))}
      </ul>
    </>
  );
}
