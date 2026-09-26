import { useMemo, useState } from "react";
import { api } from "../api.js";
import { COLUMNS, ago, fmtDate, navigate, usePoll } from "../lib.js";
import { Pill, Spinner } from "../ui.jsx";
import PostJobModal from "../components/PostJobModal.jsx";

export default function JobsPage() {
  const { data, error, loading, reload } = usePoll(() => api.jobs(), 10000);
  const [filter, setFilter] = useState("open");
  const [posting, setPosting] = useState(false);
  const jobs = useMemo(() => data?.jobs || [], [data]);
  const shown = jobs.filter((j) => filter === "all" || j.status === filter);
  const count = (s) => jobs.filter((j) => s === "all" || j.status === s).length;

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1>Jobs</h1>
          <p className="muted">Every posted role and where its candidates are. Open one to see its board.</p>
        </div>
        <button className="btn primary" onClick={() => setPosting(true)}>+ Post a job</button>
      </div>

      {error && !data && (
        <div className="banner bad">
          {error.message} {error.status === 0 && "The first load can take up to a minute while the server wakes up."}
        </div>
      )}

      <div className="jobs-filters">
        <div className="seg">
          {["open", "closed", "all"].map((s) => (
            <button key={s} className={filter === s ? "on" : ""} onClick={() => setFilter(s)}>
              {s[0].toUpperCase() + s.slice(1)} <span className="faint">{count(s)}</span>
            </button>
          ))}
        </div>
      </div>

      {loading && !data ? (
        <div className="empty row" style={{ justifyContent: "center" }}><Spinner /> Loading jobs…</div>
      ) : shown.length === 0 ? (
        <div className="card empty">
          {jobs.length === 0 ? "No jobs yet. Post your first one." : `No ${filter} jobs.`}
        </div>
      ) : (
        <div className="job-list">
          {shown.map((j) => (
            <JobRow key={j.job_id} job={j} />
          ))}
        </div>
      )}

      {posting && (
        <PostJobModal
          onClose={() => setPosting(false)}
          onPosted={(id) => {
            setPosting(false);
            reload();
            navigate(`/jobs/${encodeURIComponent(id)}`);
          }}
        />
      )}
    </main>
  );
}

function JobRow({ job }) {
  const c = job.counts || {};
  const total = job.candidate_count || 0;
  const needsYou = (c.resume_review || 0) + (c.final_review || 0);
  const href = `/jobs/${encodeURIComponent(job.job_id)}`;
  return (
    <a
      className="card job-row"
      href={href}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey) return;
        e.preventDefault();
        navigate(href);
      }}
      style={{ textDecoration: "none", color: "inherit" }}
    >
      <div style={{ minWidth: 0 }}>
        <div className="row">
          <h3 className="ellipsis">{job.title}</h3>
          <Pill value={job.status} dot />
        </div>
        <div className="meta ellipsis">
          {[job.company_name, job.location, `posted ${fmtDate(job.created_at)}`, job.posted_by && `by ${job.posted_by}`]
            .filter(Boolean).join(" · ")}
        </div>
      </div>

      <div className="funnel">
        <div className="funnel-bar">
          {total > 0 &&
            COLUMNS.map((col) =>
              c[col.key] ? (
                <span key={col.key} className={`c-${col.key}`} style={{ width: `${(c[col.key] / total) * 100}%`, background: "var(--c)" }} title={`${col.label}: ${c[col.key]}`} />
              ) : null,
            )}
        </div>
        <div className="funnel-legend">
          {total === 0 ? (
            <span className="faint">No applicants yet</span>
          ) : (
            COLUMNS.filter((col) => c[col.key]).map((col) => (
              <span key={col.key}><b>{c[col.key]}</b> {col.label.toLowerCase()}</span>
            ))
          )}
        </div>
      </div>

      <div className="job-stats">
        <div className="job-stat"><b>{total}</b><span>applicants</span></div>
        <div className={`job-stat${needsYou ? " attn" : ""}`}><b>{needsYou}</b><span>need review</span></div>
        <div className="job-stat"><b>{c.selected || 0}</b><span>selected</span></div>
        <div className="job-stat" style={{ minWidth: 70 }}><span>{job.last_activity ? `active ${ago(job.last_activity)}` : ""}</span></div>
      </div>
    </a>
  );
}
