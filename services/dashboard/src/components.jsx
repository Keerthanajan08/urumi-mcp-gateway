import React, { useEffect, useState } from "react";
import { copy } from "./api.js";

export function Lamp({ status }) {
  const label = { connected: "Connected", unreachable: "Unreachable", disabled: "Disabled", starting: "Starting" }[status] || "Checking";
  return <span className={`lamp ${status || "unknown"}`}><i aria-hidden />{label}</span>;
}

export function Modal({ title, onClose, children }) {
  useEffect(() => {
    const h = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h); return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <header><h2>{title}</h2><button className="link" onClick={onClose} aria-label="Close">Close</button></header>
        {children}
      </div>
    </div>
  );
}

export function CopyBox({ value, label }) {
  const [ok, setOk] = useState(false);
  return (
    <div className="copybox">
      <code>{value}</code>
      <button className="ghost" onClick={() => { copy(value); setOk(true); setTimeout(() => setOk(false), 1500); }}>{ok ? "Copied" : label || "Copy"}</button>
    </div>
  );
}

export function useLoad(fn, deps = [], intervalMs = 0) {
  const [state, setState] = useState({ data: null, error: "", loading: true });
  const run = React.useCallback(async () => {
    try { setState({ data: await fn(), error: "", loading: false }); }
    catch (e) { setState((s) => ({ ...s, error: e.message, loading: false })); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { run(); if (!intervalMs) return; const t = setInterval(run, intervalMs); return () => clearInterval(t); }, [run, intervalMs]);
  return { ...state, reload: run };
}
