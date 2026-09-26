import { callLine, fmtTime, num } from "../lib.js";
import { KV, Pill, Section, yesNo } from "../ui.jsx";

export function InterviewTab({ d, ov }) {
  const s3 = d.records?.stage3_calling;
  const s4 = d.records?.stage4_evaluation;
  if (!s3) {
    const st = d.entry?.stages?.stage3_calling;
    return (
      <div className="empty">
        {st?.status === "awaiting" ? "Invited: the candidate hasn't finished the screening call yet." : "No screening call yet."}
      </div>
    );
  }
  const sc = s3.screening || {};
  const answers = d.answers || [];

  return (
    <>
      {s4 && <Evaluation s4={s4} ov={ov} />}

      <Section title="Screening call" right={sc.sentiment && <Pill tone={sc.sentiment === "positive" ? "ok" : sc.sentiment === "negative" ? "bad" : ""} value={sc.sentiment} />}>
        <div className="small muted">{callLine(s3.call)}{s3.call?.ended_at ? ` · ${fmtTime(s3.call.ended_at)}` : ""}</div>
        {sc.overall_summary && <p>{sc.overall_summary}</p>}
        <KV rows={[
          ["Interested", yesNo(sc.interested_in_role)],
          ["Notice period", sc.notice_period_days != null && `${sc.notice_period_days} days`],
          ["Relocate / hybrid", yesNo(sc.willing_to_relocate)],
          ["Current CTC", sc.current_ctc],
          ["Expected CTC", sc.expected_ctc],
          ["Location", sc.current_location],
          ["Interview availability", sc.available_for_interview],
        ]} />
        {sc.red_flags?.length > 0 && (
          <div className="box bad small"><b>Red flags</b><ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{sc.red_flags.map((r, i) => <li key={i}>{r}</li>)}</ul></div>
        )}
        {sc.candidate_questions?.length > 0 && (
          <div className="box soft small"><b>The candidate asked</b><ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{sc.candidate_questions.map((r, i) => <li key={i}>{r}</li>)}</ul></div>
        )}
      </Section>

      <Section title={`Answers (${answers.filter((a) => a.answered).length}/${answers.length})`}>
        {answers.some((a) => a.mapping === "inferred") && (
          <div className="faint small">Older call: answers were matched to questions from the agent's wording.</div>
        )}
        {answers.length === 0 ? (
          <div className="faint small">No answers recorded.</div>
        ) : (
          answers.map((a, i) => (
            <div key={a.question_id || i} className={`answer${a.kind === "role" ? " role" : ""}`}>
              <div className="row small faint">
                Q{i + 1} · {a.kind === "role" ? "role question · scored" : "logistics"}
              </div>
              <div className="q">{a.question}</div>
              <div className={a.answered ? "" : "faint"}>{a.answered ? a.answer_text : "Not answered"}</div>
              {a.ai_summary && <div className="small muted">AI summary: {a.ai_summary}</div>}
              {a.exchange?.length > 1 && (
                <details>
                  <summary>Full exchange ({a.exchange.length} lines)</summary>
                  <div className="xchg">
                    {a.exchange.map((t, j) => (
                      <div key={j}><b>{t.speaker === "agent" ? "Agent" : "Candidate"}:</b> {t.text}</div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          ))
        )}
      </Section>

      {d.transcript && (
        <details>
          <summary>Full transcript</summary>
          <div className="transcript">{d.transcript}</div>
        </details>
      )}
    </>
  );
}

function Evaluation({ s4 }) {
  return (
    <Section title="Interview evaluation" right={<span className="pill accent">AI suggestion</span>}>
      <div className="row" style={{ gap: 14 }}>
        <span className="big-score">{num(s4.final_score, 1)}</span>
        <div className="grow small">
          <div><Pill value={s4.suggested_decision} /> <span className="faint">final score · pass mark {s4.threshold}</span></div>
          <div className="muted" style={{ marginTop: 3 }}>
            Resume {num(s4.resume_score, 0)} × {s4.resume_weight} + interview {num(s4.interview_score, 0)} × {s4.interview_weight}
          </div>
        </div>
      </div>
      {s4.needs_review && (
        <div className="box warn small"><b>Look closely before deciding:</b> {(s4.review_reasons || []).join(" · ")}</div>
      )}
      <div className="stack">
        {(s4.competencies || []).map((c) => (
          <div key={c.competency} className="crit">
            <span className="name">{c.competency.replace(/_/g, " ")} <span className="faint small" style={{ textTransform: "none", fontWeight: 400 }}>· weight {Math.round(c.weight * 100)}% · adds {c.weighted_score}</span></span>
            <span className="val">{c.score}</span>
            <div className="bar" style={{ gridColumn: "1 / -1" }}><span style={{ width: `${c.score}%` }} /></div>
            {c.rationale && <span className="ev">{c.rationale}</span>}
            {(c.evidence_quotes || []).map((q, i) => <span key={i} className="ev quote">“{q}”</span>)}
            {c.unverified_quotes?.length > 0 && (
              <span className="ev faint">{c.unverified_quotes.length} quote(s) not found in the transcript were dropped</span>
            )}
          </div>
        ))}
      </div>
      {s4.concerns?.length > 0 && (
        <div className="box soft small"><b>Concerns</b><ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{s4.concerns.map((r, i) => <li key={i}>{r}</li>)}</ul></div>
      )}
      {s4.summary && <p className="muted">{s4.summary}</p>}
      <p className="faint small">Scored by {s4.llm?.model} · {fmtTime(s4.created_at)}. Only quotes found word for word in the candidate's answers are shown.</p>
    </Section>
  );
}
