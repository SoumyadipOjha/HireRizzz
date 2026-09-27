import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import {
  COLUMN, STAGES, TONE, basename, fmtTime, initials, label, nameOf, num, requireApprover, toast,
  usePoll, waitingFinal, waitingShortlist,
} from "../lib.js";
import { CopyButton, EmailLine, KV, Pill, Section, Spinner, useEscape } from "../ui.jsx";
import { ResumeTab } from "./ResumeTab.jsx";
import { InterviewTab } from "./InterviewTab.jsx";
import { CredibilityTab } from "./CredibilityTab.jsx";

export default function CandidateDrawer({ cid, overview, jobStatus, onClose, onChanged }) {
  const entry = overview.index?.candidates?.[cid];
  const d = usePoll(() => api.candidate(cid), 0, [cid, entry?.updated_at]);
  const [tab, setTab] = useState("overview");
  useEffect(() => setTab("overview"), [cid]);
  useEscape(onClose);

  const changed = useCallback(() => {
    d.reload();
    onChanged();
  }, [d, onChanged]);

  if (!entry) {
    return (
      <Shell onClose={onClose}>
        <div className="drawer-body"><div className="empty">This candidate isn't part of this job.</div></div>
      </Shell>
    );
  }
  const data = d.data;
  const e = data?.entry || entry;
  const x = data?.records?.stage1_extraction?.extraction;
  const name = x?.full_name || nameOf(e);
  const red = data?.credibility?.summary?.red ?? e.credibility?.red ?? 0;

  return (
    <Shell onClose={onClose}>
      <div className="drawer-head">
        <div className="drawer-title">
          <span className="avatar lg">{initials(name)}</span>
          <div className="grow">
            <div className="row wrap">
              <h2>{name}</h2>
              <Pill tone={colTone(e.board_column)}>{COLUMN[e.board_column]?.label || label(e.overall_status)}</Pill>
            </div>
            <div className="sub">
              {[x?.headline || x?.current_title, x?.email, x?.phone].filter(Boolean).join(" · ") || basename(e.source_file)}
            </div>
          </div>
          <button className="btn ghost icon" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <nav className="tabs">
          {[
            ["overview", "Overview"],
            ["resume", "Resume"],
            ["interview", "Screening"],
            ["credibility", "Credibility"],
          ].map(([k, l]) => (
            <button key={k} className={`tab${tab === k ? " on" : ""}`} onClick={() => setTab(k)}>
              {l}
              {k === "credibility" && red > 0 && <span className="badge">{red}</span>}
            </button>
          ))}
        </nav>
      </div>
      <div className="drawer-body">
        {!data && d.loading ? (
          <div className="empty row" style={{ justifyContent: "center" }}><Spinner /> Loading…</div>
        ) : !data ? (
          <div className="banner bad">{d.error?.message || "Couldn't load this candidate."}</div>
        ) : tab === "overview" ? (
          <OverviewTab d={data} e={e} ov={overview} jobStatus={jobStatus} onChanged={changed} goTo={setTab} />
        ) : tab === "resume" ? (
          <ResumeTab d={data} />
        ) : tab === "interview" ? (
          <InterviewTab d={data} ov={overview} />
        ) : (
          <CredibilityTab d={data} e={e} onChanged={changed} />
        )}
      </div>
    </Shell>
  );
}

function Shell({ children, onClose }) {
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label="Candidate details">{children}</aside>
    </>
  );
}

const colTone = (c) =>
  ({ selected: "ok", rejected: "muted", fraud: "bad", resume_review: "accent", final_review: "accent", interview: "info" })[c] || "";

// ------------------------------------------------------------------ overview

function OverviewTab({ d, e, ov, onChanged, goTo }) {
  const s2 = d.records?.stage2_shortlisting;
  const s4 = d.records?.stage4_evaluation;
  const final = e.reviews?.final;

  return (
    <>
      {e.fraud_blocked && (
        <div className="box bad stack tight">
          <b>Fraud detected: this candidate is stopped</b>
          <span className="small">
            {e.credibility?.red ?? ""} issue(s) found in the resume. No scoring, invite, screening or approval until a recruiter clears it.
          </span>
          <div><button className="btn sm" onClick={() => goTo("credibility")}>Review the issues</button></div>
        </div>
      )}
      {waitingShortlist(e) && <ShortlistDecision e={e} s2={s2} ov={ov} onChanged={onChanged} />}
      {waitingFinal(e) && <FinalDecision e={e} s4={s4} ov={ov} onChanged={onChanged} />}
      {final && <DecidedFinal e={e} s4={s4} onChanged={onChanged} />}

      {(s2 || s4) && (
        <div className="scores">
          <div className="score-tile"><span>Resume score</span><b>{num(s2?.overall_score, 0)}</b></div>
          <div className="score-tile"><span>Screening score</span><b>{num(s4?.interview_score, 0)}</b></div>
          <div className="score-tile"><span>Final score</span><b>{num(s4?.final_score, 1)}</b></div>
        </div>
      )}
      {(s2 || s4) && <ScoreBreakdown s2={s2} s4={s4} />}

      <Section title="Progress">
        <div className="timeline">
          {STAGES.map(([key, l]) => {
            const s = e.stages?.[key] || {};
            const tone = TONE[s.status] || "";
            return (
              <div key={key} className="tl-item">
                <span className={`tl-dot ${tone}`} />
                <div>
                  <div className="tl-title">{l} <Pill value={s.status} /></div>
                  {(s.error || s.note || s.updated_at) && (
                    <div className={`tl-note${s.error ? " err" : ""}`}>{s.error || s.note || fmtTime(s.updated_at)}</div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </Section>

      <Decisions e={e} />
      <Invite d={d} e={e} />

      {d.failures?.length > 0 && (
        <Section title={`Failure history (${d.failures.length})`}>
          {d.failures.map((f, i) => (
            <div key={i} className="box bad small">
              <div className="faint">{fmtTime(f.ts)} · {f.stage} · run {String(f.run_id || "").slice(0, 8)}</div>
              <div className="mono">{f.error_type}: {f.error}</div>
            </div>
          ))}
        </Section>
      )}

      <Section title="File">
        <KV rows={[
          ["Resume file", basename(e.source_file)],
          ["Applied", fmtTime(e.ingested_at)],
          ["Candidate ID", <span className="row" key="id"><span className="mono">{e.candidate_id}</span><CopyButton text={e.candidate_id} /></span>],
        ]} />
      </Section>
    </>
  );
}

async function decide(gate, cid, decision, confirmText, okText, onChanged) {
  const by = requireApprover();
  if (!by) return;
  if (!window.confirm(confirmText(by))) return;
  try {
    const r = await api.approve(gate, { [cid]: decision }, by);
    toast(okText(r), "ok");
    onChanged();
  } catch (err) {
    toast(`Couldn't save: ${err.message}`, "error");
  }
}

function ShortlistDecision({ e, s2, ov, onChanged }) {
  const ai = e.stages.stage2_shortlisting.decision;
  const outbox = ov.email_mode === "outbox" ? " (outbox: written to files, not sent)" : "";
  const go = (to) =>
    decide("shortlist", e.candidate_id, to,
      (by) => `${to === "shortlisted" ? "Shortlist" : "Reject"} ${nameOf(e)} as ${by}?${to !== ai ? "\n\nThis overrides the AI's suggestion." : ""}\n\n` +
        (to === "shortlisted" ? `They get their screening link by email${outbox}.` : `They get a polite rejection email${outbox}.`),
      (r) => (to === "shortlisted" ? `Shortlisted. ${r.invited?.length ? "Screening invite sent." : ""}` : "Rejected. Email sent."),
      onChanged);
  return (
    <div className="decision-card attn">
      <div className="row between">
        <b>Resume decision</b>
        <span className="faint small">Recruiter · after resume scoring</span>
      </div>
      <div className="row" style={{ gap: 14 }}>
        <span className="big-score">{num(s2?.overall_score, 0)}</span>
        <div className="grow small">
          <div>AI suggests <Pill value={ai} /> <span className="faint">(pass mark {ov.thresholds?.shortlist_score})</span></div>
          {s2?.decision_reasons?.length > 0 && <div className="muted" style={{ marginTop: 3 }}>{s2.decision_reasons.join(" · ")}</div>}
        </div>
      </div>
      <div className="row">
        <button className="btn ok" onClick={() => go("shortlisted")}>Shortlist &amp; send invite</button>
        <button className="btn danger" onClick={() => go("rejected")}>Reject</button>
      </div>
    </div>
  );
}

function FinalDecision({ e, s4, ov, onChanged }) {
  const ai = e.stages.stage4_evaluation.decision;
  const go = (to) =>
    decide("final", e.candidate_id, to,
      (by) => `${to === "shortlisted" ? "Move" : "Reject"} ${nameOf(e)} ${to === "shortlisted" ? "to the final shortlist" : ""} as ${by}?` +
        (to !== ai ? `\n\nThis overrides the AI's suggestion${ai === "rejected" ? ` (below the pass mark of ${ov.evaluation?.final_threshold})` : ""}.` : "") +
        "\n\nNo email is sent yet: send the results from Results.",
      () => "Final decision saved. Send the result email from Results.",
      onChanged);
  return (
    <div className="decision-card attn">
      <div className="row between">
        <b>Final decision</b>
        <span className="faint small">Hiring manager · after the screening</span>
      </div>
      <div className="row" style={{ gap: 14 }}>
        <span className="big-score">{num(s4?.final_score, 1)}</span>
        <div className="grow small">
          <div>AI suggests <Pill value={ai} /> <span className="faint">(pass mark {ov.evaluation?.final_threshold})</span></div>
          {s4 && (
            <div className="muted" style={{ marginTop: 3 }}>
              Resume {num(s4.resume_score, 0)} × {s4.resume_weight} + screening {num(s4.interview_score, 0)} × {s4.interview_weight}
            </div>
          )}
        </div>
      </div>
      {ai === "rejected" && <div className="box warn small">Below the pass mark. You can still select them: your decision is recorded as an override.</div>}
      {s4?.needs_review && <div className="box warn small"><b>Look closely before deciding:</b> {(s4.review_reasons || []).join(" · ")}</div>}
      <div className="row">
        <button className="btn ok" onClick={() => go("shortlisted")}>Shortlist</button>
        <button className="btn danger" onClick={() => go("rejected")}>Reject</button>
      </div>
    </div>
  );
}

function DecidedFinal({ e, s4, onChanged }) {
  const f = e.reviews.final;
  const note = e.notifications?.final;
  const to = f.decision === "shortlisted" ? "rejected" : "shortlisted";
  const current = note && ["sent", "outbox"].includes(note.status) && note.kind === (f.decision === "shortlisted" ? "selected" : "not_selected");
  const move = () =>
    decide("final", e.candidate_id, to,
      (by) => `Move ${nameOf(e)} to the final ${to === "shortlisted" ? "shortlist" : "rejected list"} as ${by}?` +
        (to === "shortlisted" && e.stages.stage4_evaluation.decision === "rejected" ? `\n\nThey are below the pass mark (final ${num(s4?.final_score, 1)}); this overrides the AI.` : "") +
        (current ? `\n\nThey were already emailed “${label(note.kind)}”. You can email the new result afterwards.` : ""),
      () => "Decision changed.", onChanged);
  return (
    <div className={`decision-card ${f.decision === "shortlisted" ? "" : ""}`}>
      <div className="row between">
        <b>{f.decision === "shortlisted" ? "Shortlisted" : "Not shortlisted"}</b>
        <Pill value={f.decision === "shortlisted" ? "selected" : "rejected"} />
      </div>
      <div className="small muted">
        by {f.by} · {fmtTime(f.at)}{f.ai_decision && f.ai_decision !== f.decision ? ` · overrode the AI (${label(f.ai_decision)})` : ""}
        {f.note ? ` · “${f.note}”` : ""}
      </div>
      <div className="small">
        Result email: {note ? <><Pill value={note.status} /> {note.to && `to ${note.to}`} {!current && note.status !== "failed" && "(an older result)"}</> : <span className="faint">not sent yet: send it from Results</span>}
      </div>
      <div><button className="btn sm" onClick={move}>Move to {to === "shortlisted" ? "shortlist" : "rejected"}</button></div>
    </div>
  );
}

function Decisions({ e }) {
  const gates = [["shortlist", "Resume shortlist"], ["final", "Final decision"]];
  const rows = gates.filter(([g]) => e.reviews?.[g] || e.notifications?.[g]);
  const clar = e.notifications?.credibility;
  if (!rows.length && !clar) return null;
  return (
    <Section title="Decisions & emails">
      {rows.map(([g, l]) => {
        const rv = e.reviews?.[g];
        const n = e.notifications?.[g];
        return (
          <div key={g} className="box stack tight small">
            <div className="row between"><b>{l}</b>{rv && <Pill value={rv.decision} />}</div>
            {rv && (
              <div className="muted">
                {rv.by} · {fmtTime(rv.at)}{rv.ai_decision && rv.ai_decision !== rv.decision ? ` · overrode the AI (${label(rv.ai_decision)})` : ""}
                {rv.note ? ` · “${rv.note}”` : ""}
              </div>
            )}
            {n && <EmailLine n={n} />}
          </div>
        );
      })}
      {clar && (
        <div className="box stack tight small">
          <b>Clarification request</b>
          <EmailLine n={clar} />
        </div>
      )}
    </Section>
  );
}

function Invite({ d, e }) {
  const inv = d.invite;
  const st3 = e.stages?.stage3_calling || {};
  const s3 = d.records?.stage3_calling;
  if (!inv && st3.status !== "awaiting") return null;
  return (
    <Section title={s3 ? "Screening link (another attempt)" : "Screening call invite"}>
      {inv ? (
        <div className="box stack tight">
          <div className="small">
            {s3
              ? `Last call: ${label(s3.call?.status)}${s3.call?.reschedule_note ? ` (${s3.call.reschedule_note})` : ""}. The link below still works.`
              : "Shortlisted: waiting for the candidate to open their screening link."}
          </div>
          <div className="row">
            <input className="input mono" readOnly value={inv.url} onFocus={(ev) => ev.target.select()} />
            <CopyButton text={inv.url} label="Copy link" />
            <a className="btn sm" href={inv.url} target="_blank" rel="noreferrer">Open ↗</a>
          </div>
          <div className="small faint">
            Expires {fmtTime(inv.expires_at)} · {inv.sessions} attempt(s) so far. Share it only with this candidate.
          </div>
          <div className="small">
            {inv.emailed_at ? (
              <span className="muted">Invite emailed to {inv.email_to} · {fmtTime(inv.emailed_at)} · {inv.reminders_sent || 0} reminder(s)</span>
            ) : (
              <span style={{ color: "var(--bad)" }}>Not emailed: {inv.email_error || "unknown reason"}</span>
            )}
            {inv.opened_at && <span className="muted"> · opened {fmtTime(inv.opened_at)}</span>}
            {inv.verified_at && <span className="muted"> · email verified</span>}
          </div>
        </div>
      ) : (
        <div className="box soft small">{st3.note || "No active link."}</div>
      )}
    </Section>
  );
}


// ------------------------------------------------------------------ how the AI's scores add up

const nice = (s) => s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function BreakdownTable({ title, rows, total, totalLabel, passMark }) {
  return (
    <div className="bd">
      <div className="bd-head">
        <b>{title}</b>
        <span className="faint small">score × weight = points</span>
      </div>
      {rows.map((r) => (
        <div key={r.name} className="bd-row">
          <span className="bd-name">{nice(r.name)}</span>
          <span className="bd-bar"><span style={{ width: `${Math.max(0, Math.min(100, r.score))}%` }} /></span>
          <span className="bd-num"><b>{num(r.score, 0)}</b><span className="faint">/100</span></span>
          <span className="bd-w">× {Math.round(r.weight * 100)}%</span>
          <span className="bd-pts">= <b>{num(r.points, 1)}</b></span>
        </div>
      ))}
      <div className="bd-row bd-total">
        <span className="bd-name">{totalLabel}</span>
        <span className="bd-bar" />
        <span />
        <span className="bd-w">{passMark != null ? `pass ${passMark}` : ""}</span>
        <span className="bd-pts">= <b>{num(total, 1)}</b></span>
      </div>
    </div>
  );
}

function ScoreBreakdown({ s2, s4 }) {
  return (
    <Section title="Score breakdown" right={<span className="faint small" style={{ textTransform: "none", letterSpacing: 0, fontWeight: 400 }}>how the AI's scores add up</span>}>
      {s2 && (
        <BreakdownTable title="Resume score" totalLabel="Resume score" total={s2.overall_score} passMark={s2.threshold}
          rows={(s2.criteria || []).map((c) => ({ name: c.criterion, score: c.score, weight: c.weight, points: c.weighted_score }))} />
      )}
      {s4 && (
        <BreakdownTable title="Screening score" totalLabel="Screening score" total={s4.interview_score}
          rows={(s4.competencies || []).map((c) => ({ name: c.competency, score: c.score, weight: c.weight, points: c.weighted_score }))} />
      )}
      {s4 && (
        <div className="bd bd-final">
          <div className="bd-row">
            <span className="bd-name">Resume</span>
            <span className="bd-bar"><span style={{ width: `${s4.resume_score}%` }} /></span>
            <span className="bd-num"><b>{num(s4.resume_score, 0)}</b></span>
            <span className="bd-w">× {Math.round(s4.resume_weight * 100)}%</span>
            <span className="bd-pts">= <b>{num(s4.resume_score * s4.resume_weight, 1)}</b></span>
          </div>
          <div className="bd-row">
            <span className="bd-name">Screening</span>
            <span className="bd-bar"><span style={{ width: `${s4.interview_score}%` }} /></span>
            <span className="bd-num"><b>{num(s4.interview_score, 0)}</b></span>
            <span className="bd-w">× {Math.round(s4.interview_weight * 100)}%</span>
            <span className="bd-pts">= <b>{num(s4.interview_score * s4.interview_weight, 1)}</b></span>
          </div>
          <div className="bd-row bd-total">
            <span className="bd-name">Final score</span>
            <span className="bd-bar" />
            <span />
            <span className="bd-w">pass {s4.threshold}</span>
            <span className="bd-pts">= <b>{num(s4.final_score, 1)}</b></span>
          </div>
        </div>
      )}
    </Section>
  );
}
