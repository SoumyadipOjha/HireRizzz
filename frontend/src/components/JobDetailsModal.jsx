import { useState } from "react";
import { fmtDate } from "../lib.js";
import { KV, Modal } from "../ui.jsx";
import PostJobModal from "./PostJobModal.jsx";
import LinkedInModal from "./LinkedInModal.jsx";

export default function JobDetailsModal({ info, onClose, onChanged }) {
  const [editing, setEditing] = useState(false);
  const [sharing, setSharing] = useState(false);
  const j = info.job || {};
  if (sharing) return <LinkedInModal job={info.job} onClose={() => setSharing(false)} />;
  if (editing) {
    return (
      <PostJobModal
        edit={{ jobId: info.job_id, job: j, questions: info.questions, scored: info.candidate_count }}
        onClose={() => setEditing(false)}
        onPosted={() => {
          setEditing(false);
          onChanged();
          onClose();
        }}
      />
    );
  }
  const exp = j.min_experience_years != null || j.max_experience_years != null
    ? `${j.min_experience_years ?? 0}${j.max_experience_years != null ? `–${j.max_experience_years}` : "+"} years`
    : null;
  return (
    <Modal
      title={j.title}
      onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Close</button><button className="btn" onClick={() => setEditing(true)}>Edit job</button><button className="btn primary" onClick={() => setSharing(true)}>LinkedIn post</button></>}
    >
      <KV rows={[
        ["Company", j.company_name],
        ["Location", j.location],
        ["Experience", exp],
        ["Must have", (j.must_have_skills || []).join(", ")],
        ["Nice to have", (j.nice_to_have_skills || []).join(", ")],
        ["Job posting", j.apply_url && <a href={j.apply_url} target="_blank" rel="noreferrer">{j.apply_url}</a>],
        ["Posted", `${fmtDate(info.created_at)}${info.posted_by ? ` by ${info.posted_by}` : ""}`],
        ["Job ID", <span key="id" className="mono">{info.job_id}</span>],
      ]} />
      <div className="box soft" style={{ whiteSpace: "pre-wrap" }}>{j.description}</div>
      <div className="stack tight">
        <b>Screening call questions</b>
        <ol style={{ margin: 0, paddingLeft: 20 }} className="stack tight">
          {(info.questions || []).map((q) => (
            <li key={q.id}>
              {q.question} <span className="faint small">· {q.kind === "role" ? "role question, scored" : "logistics"}</span>
            </li>
          ))}
        </ol>
      </div>
    </Modal>
  );
}
