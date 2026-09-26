import { fmtTime, num } from "../lib.js";
import { KV, Pill, Section } from "../ui.jsx";

export function ResumeTab({ d }) {
  const s1 = d.records?.stage1_extraction;
  const s2 = d.records?.stage2_shortlisting;
  if (!s1 && !s2) return <div className="empty">The resume hasn't been read yet.</div>;
  const x = s1?.extraction || {};

  return (
    <>
      {s2 && (
        <Section title="Resume score" right={<span className="faint small">by {s2.llm?.model}</span>}>
          <div className="row" style={{ gap: 14 }}>
            <span className="big-score">{num(s2.overall_score, 1)}</span>
            <div className="grow small">
              <div><Pill value={s2.decision} /> <span className="faint">weighted score · pass mark {s2.threshold}</span></div>
              {s2.decision_reasons?.length > 0 && <div className="muted" style={{ marginTop: 3 }}>{s2.decision_reasons.join(" · ")}</div>}
            </div>
          </div>
          <div className="stack">
            {(s2.criteria || []).map((c) => (
              <div key={c.criterion} className="crit">
                <span className="name">{c.criterion.replace(/_/g, " ")} <span className="faint small" style={{ textTransform: "none", fontWeight: 400 }}>· weight {Math.round(c.weight * 100)}% · adds {c.weighted_score}</span></span>
                <span className="val">{c.score}</span>
                <div className="bar" style={{ gridColumn: "1 / -1" }}><span style={{ width: `${c.score}%` }} /></div>
                {c.evidence && <span className="ev">{c.evidence}</span>}
              </div>
            ))}
          </div>
          {(s2.matched_must_have_skills?.length > 0 || s2.missing_must_have_skills?.length > 0 || s2.matched_nice_to_have_skills?.length > 0) && (
            <div className="tags">
              {(s2.matched_must_have_skills || []).map((s) => <span key={`m${s}`} className="tag ok">✓ {s}</span>)}
              {(s2.missing_must_have_skills || []).map((s) => <span key={`x${s}`} className="tag bad">✗ {s}</span>)}
              {(s2.matched_nice_to_have_skills || []).map((s) => <span key={`n${s}`} className="tag">+ {s}</span>)}
            </div>
          )}
          {s2.rationale && <p className="muted">{s2.rationale}</p>}
          <p className="faint small">Scored {fmtTime(s2.created_at)}. Name, contact details and location are hidden from the scorer.</p>
        </Section>
      )}

      {s1 && (
        <Section title="Profile" right={<span className="faint small">read by {s1.llm?.model}</span>}>
          {x.summary && <p>{x.summary}</p>}
          <KV rows={[
            ["Headline", x.headline],
            ["Current role", [x.current_title, x.current_company].filter(Boolean).join(" at ")],
            ["Experience", x.total_experience_years != null && `${x.total_experience_years} years`],
            ["Email", x.email],
            ["Phone", x.phone],
            ["Location", x.location],
            ["LinkedIn", x.linkedin_url && <a href={x.linkedin_url.startsWith("http") ? x.linkedin_url : `https://${x.linkedin_url}`} target="_blank" rel="noreferrer">{x.linkedin_url}</a>],
            ["Education", (x.education || []).length > 0 && (
              <div className="stack tight">
                {x.education.map((ed, i) => (
                  <span key={i}>
                    {[ed.degree, ed.field_of_study].filter(Boolean).join(", ")}
                    {ed.institution && ` — ${ed.institution}`}
                    {ed.graduation_year && ` (${ed.graduation_year})`}
                  </span>
                ))}
              </div>
            )],
            ["Certifications", (x.certifications || []).join("; ")],
            ["Languages", (x.languages || []).join(", ")],
          ]} />
          {s1.truncated && <div className="box warn small">The resume text was truncated before it was read.</div>}
        </Section>
      )}

      {x.skills?.length > 0 && (
        <Section title={`Skills (${x.skills.length})`}>
          <div className="tags">{x.skills.map((s) => <span key={s} className="tag">{s}</span>)}</div>
        </Section>
      )}

      {x.work_experience?.length > 0 && (
        <Section title="Work experience">
          {x.work_experience.map((w, i) => (
            <div key={i} className="work">
              <b>{[w.title, w.company].filter(Boolean).join(" — ")}</b>
              <span className="small faint">
                {w.start_date || "?"} → {w.is_current ? "Present" : w.end_date || "?"}{w.location ? ` · ${w.location}` : ""}
              </span>
              {w.highlights?.length > 0 && <ul>{w.highlights.map((h, j) => <li key={j}>{h}</li>)}</ul>}
            </div>
          ))}
        </Section>
      )}
    </>
  );
}
