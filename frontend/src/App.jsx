import { useEffect, useState } from "react";
import { signOut } from "./api.js";
import { navigate, useLocation, useSessionUser } from "./lib.js";
import { Toasts } from "./ui.jsx";
import JobsPage from "./pages/JobsPage.jsx";
import BoardPage from "./pages/BoardPage.jsx";
import SystemModal from "./components/SystemModal.jsx";
import { Logo } from "./components/Logo.jsx";
import Login from "./pages/Login.jsx";

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
        <Logo onClick={(e) => (e.preventDefault(), navigate("/"))} />
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
