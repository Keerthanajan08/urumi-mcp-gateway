import React, { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api.js";

export default function Login({ onLogin }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    try { await api("/auth/login", { method: "POST", body: { email, password } }); onLogin(await api("/auth/me")); nav("/servers"); }
    catch (ex) { setErr(ex.message); } finally { setBusy(false); }
  };
  return (
    <div className="auth">
      <form onSubmit={submit} className="auth-box">
        <h1>One endpoint for every tool your team uses.</h1>
        <p className="muted">Sign in to manage MCP servers, keys and activity.</p>
        <label>Email<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {err && <div className="error" role="alert">{err}</div>}
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}

export function AcceptInvite() {
  const [params] = useSearchParams();
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [done, setDone] = useState(false);
  const nav = useNavigate();
  const submit = async (e) => {
    e.preventDefault(); setErr("");
    try { await api("/auth/accept-invite", { method: "POST", body: { token: params.get("token") || "", password } }); setDone(true); }
    catch (ex) { setErr(ex.message); }
  };
  return (
    <div className="auth">
      <form onSubmit={submit} className="auth-box">
        <h1>Set your password</h1>
        {done ? (<><p>Your account is ready.</p><button type="button" className="primary" onClick={() => nav("/login")}>Go to sign in</button></>) : (<>
          <p className="muted">Choose a password with at least 8 characters.</p>
          <label>New password<input type="password" autoComplete="new-password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus /></label>
          {err && <div className="error" role="alert">{err}</div>}
          <button className="primary">Save password</button></>)}
      </form>
    </div>
  );
}
