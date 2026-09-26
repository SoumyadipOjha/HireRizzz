import { useEffect, useMemo, useState } from "react";
import { label, navigate } from "../lib.js";
import { CountUp, Donut, Gauge } from "./Charts.jsx";

export const COL_COLOR = {
  applied: "#94a3b8", resume_review: "#6d5efc", interview: "#0ea5e9", final_review: "#a855f7",
  selected: "#10b981", rejected: "#d4d4d8", fraud: "#f43f5e",
};

const ICON = {
  users: <path d="M16 19v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9.5 10a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM21 19v-1a4 4 0 0 0-3-3.87M15.5 3.13a3.5 3.5 0 0 1 0 6.75" />,
  brief: <path d="M4 7h16v12H4zM9 7V5h6v2M4 12h16" />,
  bell: <path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0" />,
  mic: <path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3" />,
  star: <path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z" />,
  shield: <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4" />,
};

function Icon({ name }) {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      {ICON[name]}
    </svg>
  );
}

export default function Insights({ data, onPost }) {
  const t = data.totals;
  const hour = new Date().getHours();
  const greet = hour < 5 ? "Burning the midnight oil" : hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const firstJob = data.per_job?.[0]?.job_id;
  const openBoard = () => firstJob && navigate(`/jobs/${encodeURIComponent(data.per_job.slice().sort((a, b) => b.candidates - a.candidates)[0].job_id)}`);

  const kpis = [
    { k: "Candidates", v: t.candidates, icon: "users", tone: "violet" },
    { k: "Open jobs", v: t.open_jobs, icon: "brief", tone: "blue", sub: `${t.jobs} posted` },
    { k: "Need your review", v: t.need_review, icon: "bell", tone: "amber", pulse: t.need_review > 0 },
    { k: "AI interviews", v: t.interviews, icon: "mic", tone: "pink" },
    { k: "Selected", v: t.selected, icon: "star", tone: "green" },
    { k: "Fraud caught", v: t.fraud, icon: "shield", tone: "red" },
  ];

  const outcomes = [
    { key: "progress", label: "In progress", value: data.columns.applied + data.columns.resume_review + data.columns.interview + data.columns.final_review, color: "#6d5efc" },
    { key: "selected", label: "Selected", value: data.columns.selected, color: COL_COLOR.selected },
    { key: "rejected", label: "Rejected", value: data.columns.rejected, color: "#cbd5e1" },
    { key: "fraud", label: "Fraud stopped", value: data.columns.fraud, color: COL_COLOR.fraud },
  ];

  const agree = data.ai.decisions ? (data.ai.agreed / data.ai.decisions) * 100 : null;

  return (
    <section className="insights">
      <div className="hero fade-up">
        <div className="hero-glow" aria-hidden="true" />
        <div>
          <div className="eyebrow">Hiring command center</div>
          <h1>{greet}, <span className="grad-text">let's hire someone great.</span></h1>
          <p className="muted">
            {t.need_review > 0
              ? `${t.need_review} candidate${t.need_review === 1 ? " is" : "s are"} waiting for your decision.`
              : "Nothing is waiting on you right now. The AI keeps screening in the background."}
          </p>
        </div>
        <div className="hero-actions">
          {t.need_review > 0 && <button className="btn" onClick={openBoard}>Review now →</button>}
          <button className="btn primary shine" onClick={onPost}>+ Post a job</button>
        </div>
      </div>

      <div className="kpis">
        {kpis.map((x, i) => (
          <div key={x.k} className={`kpi card fade-up tone-${x.tone}`} style={{ "--i": i }}>
            <span className={`kpi-icon${x.pulse ? " pulse" : ""}`}><Icon name={x.icon} /></span>
            <b className="kpi-val"><CountUp value={x.v} /></b>
            <span className="kpi-label">{x.k}{x.sub && <em> · {x.sub}</em>}</span>
          </div>
        ))}
      </div>

      <div className="chart-grid">
        <div className="card chart-card fade-up" style={{ "--i": 2 }}>
          <div className="chart-head"><h3>Outcomes</h3><span className="faint small">hover a slice</span></div>
          <div className="chart-body"><Donut segments={outcomes} size={170} thickness={22} centerLabel="candidates" onSelect={openBoard} /></div>
        </div>

        <div className="card chart-card fade-up" style={{ "--i": 3 }}>
          <div className="chart-head"><h3>AI × human agreement</h3><span className="faint small">{data.ai.decisions} decisions</span></div>
          <div className="chart-body">
            <Gauge value={agree} label="agreed with the AI"
              sub={data.ai.decisions ? `${data.ai.overrides} override${data.ai.overrides === 1 ? "" : "s"}: people stay in charge` : "No decisions yet"} />
          </div>
        </div>

        <Facts data={data} />

      </div>
    </section>
  );
}

// ------------------------------------------------------------------ "did you know"

function facts(d) {
  const t = d.totals, out = [];
  if (d.hours_saved > 0)
    out.push({ big: `${d.hours_saved}h`, text: `of recruiter time saved. That's ${d.resume_scores.length} resumes the AI read (≈${d.assumptions.minutes_per_resume} min each by hand) and ${t.interviews} phone screens it held (≈${d.assumptions.minutes_per_screen} min each).`, emoji: "⏱️" });
  if (t.fraud > 0)
    out.push({ big: `${Math.round((t.fraud / Math.max(1, t.candidates)) * 100)}%`, text: `of applicants were stopped for resume fraud before anyone spent a minute on them (${t.fraud} of ${t.candidates}).`, emoji: "🕵️" });
  if (d.fraud_checks.length)
    out.push({ big: `#1`, text: `fraud signal: “${label(d.fraud_checks[0].check)}”, caught ${d.fraud_checks[0].count} time${d.fraud_checks[0].count === 1 ? "" : "s"}.`, emoji: "🚩" });
  if (d.ai.decisions)
    out.push({ big: `${Math.round((d.ai.agreed / d.ai.decisions) * 100)}%`, text: `of ${d.ai.decisions} human decisions matched the AI's suggestion. ${d.ai.overrides} time${d.ai.overrides === 1 ? "" : "s"} a person knew better.`, emoji: "🤝" });
  if (d.median_hours_to_decision != null)
    out.push({ big: d.median_hours_to_decision < 48 ? `${d.median_hours_to_decision}h` : `${Math.round(d.median_hours_to_decision / 24)}d`, text: "median time from application to final decision. Traditional hiring averages weeks.", emoji: "🚀" });
  if (d.calls.avg_seconds)
    out.push({ big: `${Math.floor(d.calls.avg_seconds / 60)}m ${d.calls.avg_seconds % 60}s`, text: `average AI screening call, across ${d.calls.count} call${d.calls.count === 1 ? "" : "s"}. Candidates take them whenever they like, day or night.`, emoji: "🎙️" });
  if (d.resume_scores.length) {
    const avg = Math.round(d.resume_scores.reduce((a, b) => a + b, 0) / d.resume_scores.length);
    out.push({ big: `${Math.max(...d.resume_scores)}`, text: `top resume score so far. The average is ${avg}, and ${d.resume_scores.filter((s) => s >= d.pass_marks.resume).length} cleared the pass mark of ${d.pass_marks.resume}.`, emoji: "🏆" });
  }
  if (t.emails > 0) out.push({ big: `${t.emails}`, text: "emails sent automatically: invites, results and fraud follow-ups. Zero typed by hand.", emoji: "✉️" });
  if (!out.length) out.push({ big: "0 → 1", text: "Post a job and upload a few resumes: this panel fills up with live facts about your hiring.", emoji: "✨" });
  return out;
}

function Facts({ data }) {
  const list = useMemo(() => facts(data), [data]);
  const [i, setI] = useState(0);
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused || list.length < 2) return undefined;
    const t = setInterval(() => setI((x) => (x + 1) % list.length), 750);
    return () => clearInterval(t);
  }, [paused, list.length]);
  const f = list[i % list.length];
  return (
    <div className="card chart-card facts fade-up" style={{ "--i": 7 }} onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      <div className="chart-head"><h3>Did you know?</h3><span className="faint small">{(i % list.length) + 1}/{list.length}</span></div>
      <div className="fact" key={i}>
        <span className="fact-emoji">{f.emoji}</span>
        <b className="fact-big grad-text">{f.big}</b>
        <p>{f.text}</p>
      </div>
      <div className="dots">
        {list.map((_, j) => (
          <button key={j} className={j === i % list.length ? "on" : ""} onClick={() => setI(j)} aria-label={`Fact ${j + 1}`} />
        ))}
      </div>
    </div>
  );
}
