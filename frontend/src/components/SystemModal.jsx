import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { basename, fmtTime, usePoll } from "../lib.js";
import { Modal } from "../ui.jsx";

/** Failures and the pipeline log (for whoever runs the system). */
export default function SystemModal({ onClose }) {
  const [tab, setTab] = useState("failures");
  return (
    <Modal
      wide
      title="Activity & log"
      onClose={onClose}
      extra={
        <div className="seg">
          <button className={tab === "failures" ? "on" : ""} onClick={() => setTab("failures")}>Failures</button>
          <button className={tab === "log" ? "on" : ""} onClick={() => setTab("log")}>Pipeline log</button>
        </div>
      }
    >
      {tab === "failures" ? <Failures /> : <Log />}
    </Modal>
  );
}

function Failures() {
  const f = usePoll(() => api.failures(), 10000);
  if (!f.data) return <div className="faint">{f.error ? f.error.message : "Loading…"}</div>;
  if (!f.data.length) return <div className="empty">No failures recorded. 🎉</div>;
  return (
    <div className="card" style={{ overflowX: "auto" }}>
      <table className="table">
        <thead><tr><th>When</th><th>Stage</th><th>Candidate</th><th>Error</th></tr></thead>
        <tbody>
          {f.data.map((x, i) => (
            <tr key={i}>
              <td className="small" style={{ whiteSpace: "nowrap" }}>{fmtTime(x.ts)}</td>
              <td className="small">{x.stage}</td>
              <td className="small">{basename(x.source_file)}<div className="mono faint">{String(x.candidate_id || "").slice(0, 8)}</div></td>
              <td className="mono" style={{ color: "var(--bad)" }}>{x.error_type}: {x.error}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Log() {
  const l = usePoll(() => api.log(400), 5000);
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 60) el.scrollTop = el.scrollHeight;
  }, [l.data]);
  const lines = (l.data?.log || (l.error ? "(could not load the log)" : "Loading…")).split("\n");
  return (
    <div className="log" ref={ref}>
      {lines.map((line, i) => {
        const m = line.match(/ (ERROR|WARNING|INFO|DEBUG) /);
        return <div key={i} className={m ? m[1] : ""}>{line}</div>;
      })}
    </div>
  );
}
