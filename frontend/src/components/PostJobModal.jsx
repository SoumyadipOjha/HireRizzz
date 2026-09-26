import { useEffect, useState } from "react";
import { api } from "../api.js";
import { fmtTime, requireApprover, toast } from "../lib.js";
import { Modal, Spinner } from "../ui.jsx";

const list = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
const numOrNull = (s) => (String(s).trim() === "" ? null : Number(s));

/** Post a job: brief -> AI draft -> edit -> post. With `edit`, it edits a posted job instead. */
export default function PostJobModal({ onClose, onPosted, edit = null }) {
  const [brief, setBrief] = useState("");
  const [company, setCompany] = useState("");
  const [draft, setDraft] = useState(edit ? { job: edit.job, questions: edit.questions } : null);
  const [busy, setBusy] = useState(null);
  const [secs, setSecs] = useState(0);

  useEffect(() => {
    if (busy !== "draft") return undefined;
    setSecs(0);
    const t = setInterval(() => setSecs((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, [busy]);

  useEffect(() => {
    if (edit) return;
    api.jdDraft().then((r) => {
      if (r?.draft) {
        setDraft(r.draft);
        setBrief(r.draft.brief || "");
        setCompany(r.draft.job?.company_name || "");
      }
    }).catch(() => {});
    api.jobs().then((r) => setCompany((c) => c || r?.jobs?.[0]?.company_name || "")).catch(() => {});
  }, [edit]);

  async function write() {
    setBusy("draft");
    try {
      const r = await api.writeJd(brief, company);
      setDraft(r.draft);
      toast("Draft ready. Review it, then post the job.", "ok");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(null);
    }
  }

  async function post() {
    const by = requireApprover();
    if (!by) return;
    const j = draft.job;
    const job = {
      ...j,
      company_name: j.company_name || company,
      min_experience_years: numOrNull(j.min_experience_years ?? ""),
      max_experience_years: numOrNull(j.max_experience_years ?? ""),
      must_have_skills: Array.isArray(j.must_have_skills) ? j.must_have_skills : list(j.must_have_skills || ""),
      nice_to_have_skills: Array.isArray(j.nice_to_have_skills) ? j.nice_to_have_skills : list(j.nice_to_have_skills || ""),
      apply_url: (j.apply_url || "").trim() || undefined,
    };
    delete job.approved_by;
    delete job.approved_at;
    const msg = edit
      ? `Save changes to “${job.title}” as ${by}?` +
        (edit.scored ? `\n\n${edit.scored} candidate(s) were already scored against the current version; they are not re-scored automatically.` : "")
      : `Post “${job.title}” as ${by}?\n\nIt gets its own board. Upload resumes to it and they're scored against this description; the screening agent asks these questions.`;
    if (!window.confirm(msg)) return;
    setBusy("post");
    try {
      const r = await api.postJob(job, draft.questions, by, edit?.jobId);
      toast(edit ? "Job updated." : `Posted ${r.title}.`, "ok");
      onPosted(r.job_id);
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(null);
    }
  }

  const setJob = (k, v) => setDraft((d) => ({ ...d, job: { ...d.job, [k]: v } }));
  const setQ = (i, v) => setDraft((d) => ({ ...d, questions: d.questions.map((q, j) => (j === i ? { ...q, question: v } : q)) }));
  const j = draft?.job;

  return (
    <Modal
      wide
      title={edit ? `Edit ${edit.job.title}` : "Post a job"}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!draft || !!busy} onClick={post}>
            {busy === "post" ? <><Spinner /> Saving…</> : edit ? "Save changes" : "Post job"}
          </button>
        </>
      }
    >
      {!edit && (
        <div className="stack">
          <label className="field">
            <span>Describe the role in your own words: what they'll do, must-have skills, experience, location</span>
            <textarea className="textarea" rows={4} maxLength={6000} value={brief} onChange={(e) => setBrief(e.target.value)}
              placeholder="e.g. We need a data engineer in Pune with 2-5 years of Python, SQL and Airflow to build our pipelines…" />
          </label>
          <div className="row">
            <label className="field grow"><span>Company</span>
              <input className="input" maxLength={80} value={company} onChange={(e) => setCompany(e.target.value)} />
            </label>
            <button className="btn" style={{ alignSelf: "flex-end" }} disabled={busy === "draft" || brief.trim().length < 20} onClick={write}>
              {busy === "draft" ? <><Spinner /> Drafting… {secs}s</> : draft ? "Draft again with AI" : "Draft with AI"}
            </button>
          </div>
          {busy === "draft" && (
            <div className="draft-progress">
              <div className="draft-bar"><span style={{ width: `${Math.min(95, 100 * (1 - Math.exp(-secs / 9)))}%` }} /></div>
              <span className="faint small">
                {secs < 12 ? "The AI is writing the description, skills and interview questions: usually 5–15 seconds."
                  : secs < 30 ? "Still writing. The main AI model may be busy, so a backup model is answering."
                  : "Taking longer than usual: the server may be waking up after a quiet spell (up to a minute)."}
              </span>
            </div>
          )}
        </div>
      )}

      {draft && j && (
        <div className="stack" style={{ borderTop: edit ? 0 : "1px solid var(--border)", paddingTop: edit ? 0 : 16 }}>
          {!edit && (
            <div className="row between">
              <b>AI draft · not posted yet</b>
              <span className="faint small">{draft.llm} {draft.created_at && `· ${fmtTime(draft.created_at)}`}</span>
            </div>
          )}
          {draft.language_notes?.length > 0 && (
            <div className="box accent small">
              <b>Wording the AI changed from your brief</b>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{draft.language_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
            </div>
          )}
          <div className="grid2">
            <label className="field"><span>Title</span><input className="input" maxLength={120} value={j.title || ""} onChange={(e) => setJob("title", e.target.value)} /></label>
            <label className="field"><span>Location</span><input className="input" value={j.location || ""} onChange={(e) => setJob("location", e.target.value)} /></label>
          </div>
          <div className="grid3">
            <label className="field"><span>Min experience (years)</span><input className="input" type="number" min="0" step="0.5" value={j.min_experience_years ?? ""} onChange={(e) => setJob("min_experience_years", e.target.value)} /></label>
            <label className="field"><span>Max experience (years)</span><input className="input" type="number" min="0" step="0.5" value={j.max_experience_years ?? ""} onChange={(e) => setJob("max_experience_years", e.target.value)} /></label>
            <label className="field"><span>Company</span><input className="input" maxLength={80} value={j.company_name || ""} onChange={(e) => setJob("company_name", e.target.value)} /></label>
          </div>
          <label className="field"><span>Must-have skills (comma-separated)</span>
            <input className="input" value={Array.isArray(j.must_have_skills) ? j.must_have_skills.join(", ") : j.must_have_skills || ""} onChange={(e) => setJob("must_have_skills", e.target.value)} />
          </label>
          <label className="field"><span>Nice-to-have skills (comma-separated)</span>
            <input className="input" value={Array.isArray(j.nice_to_have_skills) ? j.nice_to_have_skills.join(", ") : j.nice_to_have_skills || ""} onChange={(e) => setJob("nice_to_have_skills", e.target.value)} />
          </label>
          <label className="field"><span>Description</span>
            <textarea className="textarea" rows={8} value={j.description || ""} onChange={(e) => setJob("description", e.target.value)} />
          </label>
          <label className="field"><span>Public job posting link (optional: the "Reapply" button in fraud emails)</span>
            <input className="input" placeholder="https://careers.example.com/jobs/…" value={j.apply_url || ""} onChange={(e) => setJob("apply_url", e.target.value)} />
          </label>
          <div className="stack tight">
            <b>Screening call questions</b>
            {draft.questions.map((q, i) => (
              <label key={q.id} className="field">
                <span>{q.kind === "role" ? "Role question (scored)" : "Logistics question"} · {q.id}</span>
                <textarea className="textarea" rows={2} value={q.question} onChange={(e) => setQ(i, e.target.value)} />
              </label>
            ))}
          </div>
        </div>
      )}
    </Modal>
  );
}
