import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Spinner } from "../ui.jsx";
import { LogoMark } from "../components/Logo.jsx";

const WORDS = ["screens", "interviews", "fact-checks", "shortlists", "hires"];

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [w, setW] = useState(0);

  useEffect(() => {
    const t = setInterval(() => setW((x) => (x + 1) % WORDS.length), 2200);
    return () => clearInterval(t);
  }, []);

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
    <div className="login-page">
      <aside className="login-art" aria-hidden="true">
        <div className="blob b1" /><div className="blob b2" /><div className="blob b3" />
        <div className="grid-bg" />
        <div className="art-content">
          <div className="art-brand"><LogoMark size={44} /><span>Hire<b>Rizz</b></span></div>
          <h2>
            The AI recruiter that<br />
            <span className="rotator"><span key={w} className="rotator-word">{WORDS[w]}</span></span>
            <br />while you sleep.
          </h2>
          <p>Resume scoring, voice screening calls, fraud checks and result emails. You make the calls that matter.</p>
          <div className="floaters">
            <div className="floater f1"><span className="fl-dot green" />Arjun M. · Final score <b>82</b></div>
            <div className="floater f2"><span className="fl-dot red" />Fraud caught · <b>founded 2015</b>, joined 2012</div>
            <div className="floater f3"><span className="fl-dot violet" />AI call finished · <b>4m 12s</b></div>
          </div>
        </div>
      </aside>

      <main className="login-side">
        <form className="login-form" onSubmit={submit}>
          <div className="login-mark"><LogoMark size={48} /></div>
          <div>
            <h1>Welcome back</h1>
            <p className="muted">Sign in to your hiring command center.</p>
          </div>
          <label className="field">
            <span>Username</span>
            <input className="input lg" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
          </label>
          <label className="field">
            <span>Password</span>
            <div className="pw">
              <input className="input lg" type={show ? "text" : "password"} autoComplete="current-password" value={password}
                onChange={(e) => setPassword(e.target.value)} />
              <button type="button" className="pw-toggle" onClick={() => setShow((s) => !s)}>{show ? "Hide" : "Show"}</button>
            </div>
          </label>
          {error && <div className="banner bad shake" style={{ margin: 0 }}>{error}</div>}
          <button className="btn primary shine lg" disabled={busy || !username || !password}>
            {busy ? <><Spinner /> Signing in…</> : "Sign in →"}
          </button>
          <p className="faint small center">Candidates don't need an account: their screening link is their key.</p>
        </form>
      </main>
    </div>
  );
}
