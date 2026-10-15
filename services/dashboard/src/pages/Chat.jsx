import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Each tool call is drawn as the path it took: you -> gateway -> server -> tool.
function Route({ call }) {
  const [server, ...rest] = call.tool.split("__");
  const tool = rest.join("__") || call.tool;
  return (
    <details className={`route ${call.is_error ? "bad" : "good"}`}>
      <summary>
        <span className="hop">You</span><span className="wire" /><span className="hop">Gateway</span><span className="wire" />
        <span className="hop">{rest.length ? server : "?"}</span><span className="wire" /><span className="hop tool"><code>{tool}</code></span>
        {call.via && <span className="muted small">via search</span>}
        <span className="took">{call.duration_ms} ms</span>
      </summary>
      <div className="route-body">
        <div className="muted small">Arguments</div><pre>{JSON.stringify(call.arguments, null, 2)}</pre>
        <div className="muted small">{call.is_error ? "Error" : "Result"}</div><pre>{call.result_preview}</pre>
      </div>
    </details>
  );
}

export default function Chat() {
  const [msgs, setMsgs] = useState([]); const [text, setText] = useState(""); const [busy, setBusy] = useState(false);
  const end = useRef(null);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [msgs, busy]);

  const send = async (e) => {
    e.preventDefault(); const t = text.trim(); if (!t || busy) return;
    const next = [...msgs, { role: "user", content: t }]; setMsgs(next); setText(""); setBusy(true);
    try {
      const r = await api("/chat", { method: "POST", body: { messages: next.map(({ role, content }) => ({ role, content })) } });
      setMsgs([...next, { role: "assistant", content: r.reply, calls: r.tool_calls }]);
    } catch (ex) { setMsgs([...next, { role: "assistant", content: ex.message, failed: true }]); }
    finally { setBusy(false); }
  };

  return (
    <div className="chat">
      <header className="page-head"><div><h1>Chat</h1><p className="muted">Ask about your store. The agent uses the same tools and permissions as your API key, and its calls show up in Activity under your name.</p></div></header>
      <div className="thread" aria-live="polite">
        {msgs.length === 0 && (
          <div className="suggest">
            {["Create a product called Blue Mug for 12.50", "What orders came in today?", "Summarise today's orders and save them as a note"].map((s) => (
              <button key={s} className="ghost" onClick={() => setText(s)}>{s}</button>))}
          </div>)}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role} ${m.failed ? "failed" : ""}`}>
            {m.calls?.map((c, j) => <Route key={j} call={c} />)}
            <div className="bubble">{m.content || <span className="muted">(no answer)</span>}</div>
          </div>))}
        {busy && <div className="msg assistant"><div className="bubble muted">Working…</div></div>}
        <div ref={end} />
      </div>
      <form className="composer" onSubmit={send}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask something…" aria-label="Message" disabled={busy} />
        <button className="primary" disabled={busy || !text.trim()}>Send</button>
      </form>
    </div>
  );
}
