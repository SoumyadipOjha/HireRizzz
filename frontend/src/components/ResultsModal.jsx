import { useState } from "react";
import { api } from "../api.js";
import { label, num, requireApprover, toast, usePoll } from "../lib.js";
import { Modal, Pill, Spinner } from "../ui.jsx";

const BLOCKS = [
  ["shortlisted", "Final shortlist"],
  ["rejected", "Rejected"],
  ["awaiting_approval", "Waiting for the hiring manager"],
];

export default function ResultsModal({ jobId, overview, onClose, onChanged, onOpen }) {
  const r = usePoll(() => api.results(jobId), 0, [jobId]);
  const [sending, setSending] = useState(false);
  const data = r.data;
  const decided = data ? [...data.shortlisted, ...data.rejected] : [];
  const unsent = decided.filter((x) => !x.email_current);
  const outbox = overview?.email_mode === "outbox" ? "\n\n(outbox: written to files, not sent)" : "";

  async function send() {
    if (!window.confirm(`Email ${unsent.length} candidate(s) their final result now?${outbox}`)) return;
    setSending(true);
    try {
      const res = await api.sendResults(jobId);
      const st = Object.values(res.emails || {});
      const bad = st.filter((s) => s === "failed" || s === "skipped").length;
      toast(`${st.length - bad} email(s) sent${bad ? `, ${bad} could not be sent (see each candidate)` : ""}.`, bad ? "error" : "ok");
      r.reload();
      onChanged();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSending(false);
    }
  }

  async function move(row, to) {
    const by = requireApprover();
    if (!by) return;
    let msg = `Move ${row.name} to the final ${to === "shortlisted" ? "shortlist" : "rejected list"} as ${by}?`;
    if (to === "shortlisted" && row.ai_suggestion === "rejected") msg += `\n\n${row.name} is below the pass mark (final ${num(row.final_score, 1)}); this overrides the AI.`;
    if (row.email_current) msg += `\n\nThey were already emailed “${label(row.email_kind)}”. You can email the new result afterwards.`;
    if (!window.confirm(msg)) return;
    try {
      await api.approve("final", { [row.candidate_id]: to }, by);
      toast("Decision saved.", "ok");
      r.reload();
      onChanged();
    } catch (e) {
      toast(`Couldn't save: ${e.message}`, "error");
    }
  }

  return (
    <Modal
      wide
      title="Results"
      onClose={onClose}
      extra={
        <div className="row">
          <button className="btn sm" onClick={() => api.downloadResultsCsv(jobId).catch((e) => toast(`Download failed: ${e.message}`, "error"))}>Download CSV</button>
          <button className="btn sm primary" disabled={!unsent.length || sending} onClick={send}>
            {sending ? <><Spinner /> Sending…</> : unsent.length ? `Email ${unsent.length} result(s)` : "All results emailed"}
          </button>
        </div>
      }
    >
      {!data ? (
        r.error ? <div className="banner bad">{r.error.message}</div> : <div className="empty row" style={{ justifyContent: "center" }}><Spinner /> Loading…</div>
      ) : (
        BLOCKS.filter(([k]) => k !== "awaiting_approval" || data[k].length).map(([k, title]) => (
          <div key={k} className="stack tight">
            <div className="row"><b>{title}</b><span className="faint">{data[k].length}</span></div>
            {data[k].length === 0 ? (
              <div className="faint small">Nobody yet.</div>
            ) : (
              <div className="card" style={{ overflowX: "auto" }}>
                <table className="table">
                  <thead>
                    <tr><th>Candidate</th><th>Final</th><th>Resume</th><th>Interview</th><th>AI suggested</th><th>Decided by</th><th>Result email</th><th /></tr>
                  </thead>
                  <tbody>
                    {data[k].map((row) => (
                      <tr key={row.candidate_id}>
                        <td>
                          <a href="#" onClick={(e) => (e.preventDefault(), onOpen(row.candidate_id))}><b>{row.name}</b></a>
                          <div className="small faint">{row.email || "no email"}{row.needs_review ? " · needs review" : ""}</div>
                        </td>
                        <td><b>{num(row.final_score, 1)}</b></td>
                        <td>{num(row.resume_score, 0)}</td>
                        <td>{num(row.interview_score, 0)}</td>
                        <td><Pill value={row.ai_suggestion} /></td>
                        <td className="small">{row.decided_by || "—"}{row.overridden && <div className="faint">overrode the AI</div>}</td>
                        <td>
                          {row.email_current ? <Pill value={row.email_status} /> : row.email_status && ["sent", "outbox"].includes(row.email_status)
                            ? <Pill tone="warn">old result sent</Pill> : row.decision ? <span className="faint small">not sent</span> : "—"}
                        </td>
                        <td>
                          {k === "awaiting_approval" ? (
                            <div className="row">
                              <button className="btn sm" onClick={() => move(row, "shortlisted")}>Select</button>
                              <button className="btn sm" onClick={() => move(row, "rejected")}>Reject</button>
                            </div>
                          ) : (
                            <button className="btn sm ghost" onClick={() => move(row, k === "shortlisted" ? "rejected" : "shortlisted")}>
                              Move to {k === "shortlisted" ? "rejected" : "shortlist"}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ))
      )}
    </Modal>
  );
}
