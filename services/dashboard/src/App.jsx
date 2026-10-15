import React, { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { api } from "./api.js";
import Login, { AcceptInvite } from "./pages/Login.jsx";
import Servers from "./pages/Servers.jsx";
import Team from "./pages/Team.jsx";
import Keys from "./pages/Keys.jsx";
import Activity from "./pages/Activity.jsx";
import Chat from "./pages/Chat.jsx";
import Stats from "./pages/Stats.jsx";
import Hosted from "./pages/Hosted.jsx";
import Stores from "./pages/Stores.jsx";

const Me = createContext(null);
export const useMe = () => useContext(Me);

function Shell({ me, onLogout }) {
  const admin = me.role === "admin";
  return (
    <Me.Provider value={me}>
      <div className="shell">
        <nav className="rail" aria-label="Main">
          <div className="brand">Gateway</div>
          <NavLink to="/servers">Servers</NavLink>
          <NavLink to="/stores">Stores</NavLink>
          <NavLink to="/chat">Chat</NavLink>
          <NavLink to="/keys">My keys</NavLink>
          <NavLink to="/activity">Activity</NavLink>
          {admin && <NavLink to="/team">Team</NavLink>}
          {admin && <NavLink to="/hosted">Hosted</NavLink>}
          {admin && <NavLink to="/stats">Health</NavLink>}
          <div className="rail-foot">
            <div className="who">{me.name || me.email}</div>
            <div className="muted small">{me.role}</div>
            <button className="link" onClick={onLogout}>Sign out</button>
          </div>
        </nav>
        <main className="main">
          <Routes>
            <Route path="/servers" element={<Servers />} />
            <Route path="/stores" element={<Stores />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/keys" element={<Keys />} />
            <Route path="/activity" element={<Activity />} />
            <Route path="/team" element={admin ? <Team /> : <Navigate to="/servers" replace />} />
            <Route path="/hosted" element={admin ? <Hosted /> : <Navigate to="/servers" replace />} />
            <Route path="/stats" element={admin ? <Stats /> : <Navigate to="/servers" replace />} />
            <Route path="*" element={<Navigate to="/servers" replace />} />
          </Routes>
        </main>
      </div>
    </Me.Provider>
  );
}

export default function App() {
  const [me, setMe] = useState(undefined); // undefined = loading, null = signed out
  const nav = useNavigate();
  useEffect(() => { api("/auth/me").then(setMe).catch(() => setMe(null)); }, []);

  const logout = async () => { await api("/auth/logout", { method: "POST" }); setMe(null); nav("/login"); };

  return (
    <Routes>
      <Route path="/accept-invite" element={<AcceptInvite />} />
      <Route path="/login" element={me ? <Navigate to="/servers" replace /> : <Login onLogin={setMe} />} />
      <Route path="/*" element={me === undefined ? <div className="boot">Loading…</div>
        : me ? <Shell me={me} onLogout={logout} /> : <Navigate to="/login" replace />} />
    </Routes>
  );
}
