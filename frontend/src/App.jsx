import { useEffect, useRef, useState } from "react";
import { navigate, setApprover, useApprover, useLocation } from "./lib.js";
import { Toasts } from "./ui.jsx";
import JobsPage from "./pages/JobsPage.jsx";
import BoardPage from "./pages/BoardPage.jsx";
import SystemModal from "./components/SystemModal.jsx";

export default function App() {
  const loc = useLocation();
  const path = loc.split("?")[0];
  const m = path.match(/^\/jobs\/([^/]+)\/?$/);
  const [system, setSystem] = useState(false);

  useEffect(() => {
    document.title = m ? "HireRizz · Job board" : "HireRizz · Jobs";
  }, [m]);

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="/" onClick={(e) => (e.preventDefault(), navigate("/"))}>
          <span className="brand-mark">H</span>
          HireRizz
        </a>
        <span className="spacer" />
        <button className="btn ghost sm" onClick={() => setSystem(true)}>Activity &amp; log</button>
        <ApproverChip />
      </header>
      {m ? <BoardPage jobId={decodeURIComponent(m[1])} /> : <JobsPage />}
      {system && <SystemModal onClose={() => setSystem(false)} />}
      <Toasts />
    </div>
  );
}

function ApproverChip() {
  const name = useApprover();
  const ref = useRef(null);
  useEffect(() => {
    const focus = () => ref.current?.focus();
    window.addEventListener("hirerizz:focus-approver", focus);
    return () => window.removeEventListener("hirerizz:focus-approver", focus);
  }, []);
  return (
    <label className={`approver${name.trim() ? "" : " empty"}`} title="Your name is recorded with every decision you make">
      Approving as
      <input ref={ref} value={name} maxLength={80} placeholder="Your name" onChange={(e) => setApprover(e.target.value)} />
    </label>
  );
}
