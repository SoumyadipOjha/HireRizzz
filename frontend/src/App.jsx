import { useEffect, useState } from "react";
import { api, signOut } from "./api.js";
import { navigate, useLocation, useSessionUser } from "./lib.js";
import { Spinner, Toasts } from "./ui.jsx";
import JobsPage from "./pages/JobsPage.jsx";
import BoardPage from "./pages/BoardPage.jsx";
import SystemModal from "./components/SystemModal.jsx";

export default function App() {
  const user = useSessionUser();
  const loc = useLocation();
  const path = loc.split("?")[0];
  const m = path.match(/^\/jobs\/([^/]+)\/?$/);
  const [system, setSystem] = useState(false);

  useEffect(() => {
    document.title = !user ? "HireRizz · Sign in" : m ? "HireRizz · Job board" : "HireRizz · Jobs";
  }, [m, user]);

  if (!user) return <><Login /><Toasts /></>;

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="/" onClick={(e) => (e.preventDefault(), navigate("/"))}>
          <span className="brand-mark">H</span>
          HireRizz
        </a>
        <span className="spacer" />
        <button className="btn ghost sm" onClick={() => setSystem(true)}>Failures</button>
        <span className="user-chip"><span className="avatar sm">{user[0].toUpperCase()}</span>{user}</span>
        <button className="btn ghost sm" onClick={signOut}>Sign out</button>
      </header>
      {m ? <BoardPage jobId={decodeURIComponent(m[1])} /> : <JobsPage />}
      {system && <SystemModal onClose={() => setSystem(false)} />}
      <Toasts />
    </div>
  );
}

function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(username.trim(), password);
    } catch (err) {
      setError(err.status === 0 ? "Can't reach the server. It may be waking up: try again in a minute." : err.message);
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login card" onSubmit={submit}>
        <div className="brand" style={{ cursor: "default" }}>
          <span className="brand-mark">H</span>
          HireRizz
        </div>
        <div>
          <h1>Sign in</h1>
          <p className="muted small">Recruiter and hiring manager dashboard</p>
        </div>
        <label className="field">
          <span>Username</span>
          <input className="input" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label className="field">
          <span>Password</span>
          <input className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <div className="banner bad" style={{ margin: 0 }}>{error}</div>}
        <button className="btn primary" style={{ height: 38 }} disabled={busy || !username || !password}>
          {busy ? <><Spinner /> Signing in…</> : "Sign in"}
        </button>
      </form>
    </div>
  );
}
