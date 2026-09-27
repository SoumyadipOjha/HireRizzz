import { useMemo, useRef, useState } from "react";
import { api, fileToBase64 } from "../api.js";
import {
  COLUMNS, ago, credibilityFlag, hasFailure, initials, nameOf, navigate, requireApprover, toast, usePoll, useLocation,
} from "../lib.js";
import { Pill, Spinner } from "../ui.jsx";
import CandidateDrawer from "../components/CandidateDrawer.jsx";
import ResultsModal from "../components/ResultsModal.jsx";
import JobDetailsModal from "../components/JobDetailsModal.jsx";
import LinkedInModal from "../components/LinkedInModal.jsx";
import Countdown from "../components/Countdown.jsx";

export default function BoardPage({ jobId }) {
  const ov = usePoll(() => api.overview(jobId), 5000, [jobId]);
  const job = usePoll(() => api.job(jobId), 30000, [jobId]);
  const loc = useLocation();
  const params = new URLSearchParams(loc.split("?")[1] || "");
  const selected = params.get("c");
  const share = params.get("share") === "1";
  const [search, setSearch] = useState("");
  const [modal, setModal] = useState(null); // "results" | "details"
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef(null);

  const data = ov.data;
  const info = job.data;
  const candidates = useMemo(() => {
    const all = Object.values(data?.index?.candidates || {});
    all.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    const s = search.trim().toLowerCase();
    return s ? all.filter((c) => `${nameOf(c)} ${c.source_file} ${c.candidate_id}`.toLowerCase().includes(s)) : all;
  }, [data, search]);
  const byCol = useMemo(() => {
    const m = Object.fromEntries(COLUMNS.map((c) => [c.key, []]));
    candidates.forEach((c) => (m[c.board_column] || m.applied).push(c));
    return m;
  }, [candidates]);

  const select = (cid) => navigate(`/jobs/${encodeURIComponent(jobId)}${cid ? `?c=${cid}` : ""}`);
  const refresh = () => {
    ov.reload();
    job.reload();
  };

  if ((ov.error?.status === 404 || job.error?.status === 404) && !data) {
    return (
      <main className="page">
        <div className="card empty">
          This job doesn't exist (any more). <a href="/" onClick={(e) => (e.preventDefault(), navigate("/"))}>Back to jobs</a>
        </div>
      </main>
    );
  }

  const jd = data?.job;
  const closed = info?.status === "closed";
  const processing = data?.processing;
  const exp = jd && (jd.min_experience_years != null || jd.max_experience_years != null)
    ? `${jd.min_experience_years ?? 0}${jd.max_experience_years != null ? `–${jd.max_experience_years}` : "+"} yrs`
    : null;
  const total = Object.keys(data?.index?.candidates || {}).length;
  const count = (k) => Object.values(data?.index?.candidates || {}).filter((c) => c.board_column === k).length;

  async function upload(files) {
    if (!files.length) return;
    setUploading(true);
    try {
      const payload = await Promise.all([...files].map(async (f) => ({ name: f.name, data: await fileToBase64(f) })));
      const r = await api.uploadResumes(jobId, payload);
      toast(`Uploaded ${r.saved.length} resume(s). The AI is reading and scoring them…`, "ok");
      refresh();
    } catch (e) {
      toast(`Upload failed: ${e.message}`, "error");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function toggleStatus() {
    const to = closed ? "open" : "closed";
    if (to === "closed" && !window.confirm(`Close “${jd?.title}”? No new resumes can be added. Candidates already in the pipeline carry on.`)) return;
    try {
      await api.setJobStatus(jobId, to);
      toast(to === "closed" ? "Job closed" : "Job reopened", "ok");
      refresh();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function applyAi(gate, list) {
    const by = requireApprover();
    if (!by) return;
    const stageKey = gate === "shortlist" ? "stage2_shortlisting" : "stage4_evaluation";
    const decisions = Object.fromEntries(list.map((c) => [c.candidate_id, c.stages[stageKey].decision]));
    const y = Object.values(decisions).filter((d) => d === "shortlisted").length;
    const n = list.length - y;
    const outbox = data?.email_mode === "outbox" ? " (outbox: written to files, not sent)" : "";
    const msg = gate === "shortlist"
      ? `Accept the AI's suggestion for ${list.length} candidate(s) as ${by}?\n\n${y} shortlisted candidate(s) get their screening link by email.\n${n} rejected candidate(s) get a polite rejection email${outbox}.`
      : `Accept the AI's suggestion for ${list.length} candidate(s) as ${by}?\n\n${y} to the final shortlist, ${n} rejected.\nNo emails are sent yet: you'll send them from Results.`;
    if (!window.confirm(msg)) return;
    try {
      const r = await api.approve(gate, decisions, by);
      toast(gate === "shortlist"
        ? `Saved. ${r.invited?.length ?? 0} invite(s) sent, ${r.rejected?.length ?? 0} rejection(s).`
        : `Saved ${r.recorded ?? list.length} final decision(s). Send the result emails from Results.`, "ok");
      refresh();
    } catch (e) {
      toast(`Couldn't save: ${e.message}`, "error");
    }
  }

  return (
    <main className="page wide">
      <div className="crumbs">
        <a href="/" onClick={(e) => (e.preventDefault(), navigate("/"))}>Jobs</a>
        <span>/</span>
        <span className="ellipsis">{jd?.title || jobId}</span>
      </div>

      <div className="board-head">
        <div style={{ minWidth: 0 }}>
          <h1>
            <span className="ellipsis">{jd?.title || (ov.loading ? "Loading…" : jobId)}</span>
            {info && <Pill value={info.status} dot />}
            {info?.deadline && <Countdown deadline={info.deadline} />}
          </h1>
          {jd && (
            <div className="meta">
              {[jd.company_name, jd.location, exp, jd.must_have_skills?.length && `must have: ${jd.must_have_skills.join(", ")}`]
                .filter(Boolean).join(" · ")}
            </div>
          )}
        </div>
        <div className="row">
          <button className="btn ghost" onClick={() => setModal("details")}>Job details</button>
          <button className="btn ghost" onClick={toggleStatus} disabled={!info}>{closed ? "Reopen" : "Close job"}</button>
          <button className="btn" onClick={() => setModal("results")}>Results</button>
          <input ref={fileRef} type="file" accept=".docx,.pdf" multiple hidden onChange={(e) => upload(e.target.files)} />
          <button className="btn primary" onClick={() => fileRef.current?.click()} disabled={uploading || closed}
            title={closed ? "Reopen the job to add resumes" : "Word (.docx) or PDF resumes"}>
            {uploading ? <><Spinner /> Uploading…</> : "Upload resumes"}
          </button>
        </div>
      </div>

      {ov.error && data && <div className="banner bad">Server unreachable, retrying… ({ov.error.message})</div>}
      {ov.error && !data && ov.error.status !== 404 && (
        <div className="banner">{ov.error.status === 0 ? "Waking up the server… this can take up to a minute the first time." : ov.error.message}</div>
      )}
      {data?.llm_error && <div className="banner"><b>Screening calls can't start.</b>&nbsp;{data.llm_error}</div>}
      {data?.job_error && <div className="banner bad">Config error: {data.job_error}</div>}
      {data?.demo && <div className="banner info"><b>Demo data.</b>&nbsp;Synthetic sample candidates scored by a fake AI.</div>}
      {processing?.message && (processing.running || processing.job_id === jobId || !processing.job_id) && (
        <div className={`banner ${processing.running ? "info" : ""}`}>
          {processing.running ? <Spinner /> : "✓"} {processing.message}
        </div>
      )}

      <div className="board-tools">
        <input className="input" placeholder="Search candidates" value={search} onChange={(e) => setSearch(e.target.value)} />
        <div className="stats">
          <span className="stat"><b>{total}</b>applicants</span>
          <span className="stat attn"><b>{count("resume_review") + count("final_review")}</b>need review</span>
          <span className="stat"><b>{count("interview")}</b>in screening</span>
          <span className="stat"><b>{count("selected")}</b>shortlisted</span>
          <span className={`stat${count("fraud") ? " bad" : ""}`}><b>{count("fraud")}</b>fraud stopped</span>
        </div>
      </div>

      {ov.loading && !data ? (
        <div className="empty row" style={{ justifyContent: "center" }}><Spinner /> Loading the board…</div>
      ) : (
        <div className="board">
          {COLUMNS.map((col) => {
            const list = byCol[col.key];
            const gate = col.key === "resume_review" ? "shortlist" : col.key === "final_review" ? "final" : null;
            return (
              <section key={col.key} className={`col c-${col.key}`}>
                <div className="col-head" title={col.hint}>
                  <div className="col-title">{col.label}<span className="col-count">{list.length}</span></div>
                  {gate && list.length > 0 && (
                    <button className="btn sm" onClick={() => applyAi(gate, list)}>Accept AI suggestions</button>
                  )}
                </div>
                <div className="col-body">
                  {list.length === 0 ? (
                    <div className="col-empty">{total === 0 && col.key === "applied" ? "Upload resumes to start" : "Nobody here"}</div>
                  ) : (
                    list.map((c) => (
                      <Card key={c.candidate_id} c={c} on={c.candidate_id === selected} onClick={() => select(c.candidate_id)}
                        threshold={data?.evaluation?.final_threshold} />
                    ))
                  )}
                </div>
              </section>
            );
          })}
        </div>
      )}

      {selected && data && (
        <CandidateDrawer cid={selected} overview={data} jobStatus={info?.status} onClose={() => select(null)} onChanged={refresh} />
      )}
      {modal === "results" && <ResultsModal jobId={jobId} overview={data} onClose={() => setModal(null)} onChanged={refresh} onOpen={(cid) => (setModal(null), select(cid))} />}
      {modal === "details" && info && <JobDetailsModal info={info} onClose={() => setModal(null)} onChanged={refresh} />}
      {share && info && <LinkedInModal job={info.job} justPosted onClose={() => select(null)} />}
    </main>
  );
}

function Card({ c, on, onClick }) {
  const st = c.stages || {};
  const s2 = st.stage2_shortlisting || {};
  const s4 = st.stage4_evaluation || {};
  const s3 = st.stage3_calling || {};
  const flag = credibilityFlag(c);
  const name = nameOf(c);
  let sub = ago(c.updated_at);
  if (hasFailure(c)) sub = "Something failed: open for details";
  else if (c.board_column === "applied") sub = st.stage1_extraction?.status === "success" ? "Scoring resume…" : "Reading resume…";
  else if (c.board_column === "interview")
    sub = s3.status === "awaiting" ? "Invite sent · waiting for the call" : s3.status === "success" ? "Screened · scoring…" : "Invited to screening";
  else if (c.board_column === "rejected" && s3.status === "skipped" && s3.output_path) sub = "Opted out during the call";

  return (
    <button className={`cand${on ? " on" : ""}`} onClick={onClick}>
      <div className="cand-top">
        <span className="avatar">{initials(name)}</span>
        <div className="grow">
          <div className="cand-name ellipsis">{name}</div>
          <div className={`cand-sub ellipsis${hasFailure(c) ? "" : ""}`} style={hasFailure(c) ? { color: "var(--bad)" } : undefined}>{sub}</div>
        </div>
      </div>
      {(s2.score != null || s4.score != null || flag || c.board_column === "resume_review" || c.board_column === "final_review") && (
        <div className="cand-meta">
          {s2.score != null && (
            <span className={`score ${s2.decision === "shortlisted" ? "good" : "low"}`}>Resume <b>{Math.round(s2.score)}</b></span>
          )}
          {s4.score != null && (
            <span className={`score ${s4.decision === "shortlisted" ? "good" : "low"}`}>Final <b>{Math.round(s4.score)}</b></span>
          )}
          {c.board_column === "resume_review" && s2.decision && (
            <Pill tone={s2.decision === "shortlisted" ? "ok" : "bad"}>AI: {s2.decision === "shortlisted" ? "shortlist" : "reject"}</Pill>
          )}
          {c.board_column === "final_review" && s4.decision && (
            <Pill tone={s4.decision === "shortlisted" ? "ok" : "bad"}>AI: {s4.decision === "shortlisted" ? "shortlist" : "reject"}</Pill>
          )}
          {flag && c.board_column !== "fraud" && <Pill tone={flag.tone}>{flag.text}</Pill>}
        </div>
      )}
    </button>
  );
}
