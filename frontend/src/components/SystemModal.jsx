import { api } from "../api.js";
import { basename, fmtTime, usePoll } from "../lib.js";
import { Modal } from "../ui.jsx";

/** Processing failures across all jobs (for whoever runs the system). */
export default function SystemModal({ onClose }) {
  const f = usePoll(() => api.failures(), 10000);
  return (
    <Modal wide title="Failures" onClose={onClose}>
      {!f.data ? (
        <div className="faint">{f.error ? f.error.message : "Loading…"}</div>
      ) : !f.data.length ? (
        <div className="empty">No failures recorded. 🎉</div>
      ) : (
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
      )}
    </Modal>
  );
}
