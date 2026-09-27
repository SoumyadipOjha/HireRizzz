import { useRef, useState } from "react";
import { api, fileToBase64 } from "../api.js";
import { fmtTime, label, nameOf, requireApprover, toast } from "../lib.js";
import { EmailLine, Section, Spinner } from "../ui.jsx";

const LEVEL = { red: "Issue", amber: "Flag", green: "Confirmed" };

export function CredibilityTab({ d, e, onChanged }) {
  const cred = d.credibility;
  const s = cred?.summary || {};
  const flags = cred?.flags || [];
  const order = { red: 0, amber: 1, green: 2 };
  const sorted = [...flags].sort((a, b) => order[a.level] - order[b.level]);

  return (
    <>
      {e.fraud_blocked && <FraudActions e={e} red={s.red} onChanged={onChanged} />}
      {e.fraud_cleared && !e.fraud_blocked && (
        <div className="box soft small">
          Flags cleared by <b>{e.fraud_cleared.by}</b> on {fmtTime(e.fraud_cleared.at)}
          {e.fraud_cleared.note ? `: “${e.fraud_cleared.note}”` : ""}. New issues would stop them again.
        </div>
      )}

      <Section
        title="Resume checks"
        right={cred && (
          <span className="small faint" style={{ textTransform: "none", letterSpacing: 0, fontWeight: 400 }}>
            {s.red || 0} issue(s) · {s.amber || 0} flag(s) · {s.green || 0} confirmed{s.linkedin ? " · LinkedIn compared" : ""}
          </span>
        )}
      >
        {!cred ? (
          <div className="faint small">Checks run once the resume has been read.</div>
        ) : sorted.length === 0 ? (
          <div className="box ok small">No problems found in the resume's dates and claims.</div>
        ) : (
          sorted.map((f, i) => (
            <div key={i} className={`flag ${f.level}`}>
              <span className="flag-lvl">{LEVEL[f.level] || f.level}</span>
              <div>
                <div className="flag-check">{label(f.check)}</div>
                <div className="flag-msg">{f.message}</div>
                {f.resume && <div className="flag-src">Resume: {f.resume}</div>}
                {f.linkedin && <div className="flag-src">LinkedIn: {f.linkedin}</div>}
              </div>
            </div>
          ))
        )}
      </Section>

      <LinkedIn e={e} summary={s} file={cred?.linkedin_file} onChanged={onChanged} />

      <p className="faint small">
        Flags are for a person to look into, never an automatic rejection. LinkedIn is self-reported too: before an offer,
        verify employment (EPFO/UAN or a background check).
      </p>
    </>
  );
}

function LinkedIn({ e, summary, file, onChanged }) {
  const ref = useRef(null);
  const [busy, setBusy] = useState(false);
  const ready = e.stages?.stage1_extraction?.status === "success";

  async function upload(f) {
    if (!f) return;
    setBusy(true);
    try {
      const r = await api.uploadLinkedin(e.candidate_id, f.name, await fileToBase64(f));
      const sm = r.summary || {};
      toast(`LinkedIn compared: ${sm.red} issue(s), ${sm.amber} flag(s), ${sm.green} confirmed.`, sm.red > 0 ? "error" : "ok");
      onChanged();
    } catch (err) {
      toast(`LinkedIn check failed: ${err.message}`, "error");
    } finally {
      setBusy(false);
      if (ref.current) ref.current.value = "";
    }
  }

  return (
    <Section title="LinkedIn cross-check">
      <div className="box stack tight">
        <div className="small muted">
          {summary.linkedin
            ? `Compared with ${file || "the uploaded profile"}.`
            : "Ask the candidate for their LinkedIn PDF: LinkedIn → their profile → More (or Resources) → Save to PDF. It's compared with the resume: names, employers, dates and titles."}
        </div>
        <div>
          <input ref={ref} type="file" accept=".pdf" hidden onChange={(ev) => upload(ev.target.files[0])} />
          <button className="btn sm" disabled={busy || !ready} onClick={() => ref.current?.click()}
            title={ready ? "" : "The resume has to be read first"}>
            {busy ? <><Spinner /> Comparing with the resume…</> : summary.linkedin ? "Replace LinkedIn PDF" : "Upload LinkedIn PDF"}
          </button>
        </div>
      </div>
    </Section>
  );
}

function FraudActions({ e, red, onChanged }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(null);
  const clar = e.notifications?.credibility;
  const sent = clar && ["sent", "outbox"].includes(clar.status);

  async function email() {
    if (!window.confirm(`Email ${nameOf(e)} the ${red ?? ""} mismatch(es) and ask them to fix their resume and reapply?`)) return;
    setBusy("email");
    try {
      const n = await api.requestClarification(e.candidate_id);
      toast(["sent", "outbox"].includes(n.status) ? `Email ${n.status} to ${n.to}.` : `Not sent: ${n.error || n.status}`,
        ["sent", "outbox"].includes(n.status) ? "ok" : "error");
      onChanged();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(null);
    }
  }

  async function clear() {
    const by = requireApprover();
    if (!by) return;
    if (!note.trim()) {
      toast("Say why the flags are innocent before clearing them.", "error");
      return;
    }
    if (!window.confirm(`Clear the fraud flag for ${nameOf(e)} as ${by}? They'll continue to resume scoring, and your name and reason are recorded.`)) return;
    setBusy("clear");
    try {
      await api.clearFraud(e.candidate_id, by, note.trim());
      toast("Cleared. Their resume is being scored now.", "ok");
      onChanged();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="box bad stack">
      <div>
        <b>Fraud detected</b>
        <div className="small">
          {red ?? ""} issue(s) below. This candidate is stopped: no scoring, invite, screening or approval.
        </div>
      </div>
      <div className="stack tight">
        <div className="small">
          {clar ? <EmailLine n={clar} /> : "The candidate hasn't been asked to fix their resume yet."}
        </div>
        <div>
          <button className="btn sm" onClick={email} disabled={!!busy}>
            {busy === "email" ? <Spinner /> : null} {sent ? "Resend email" : "Email the candidate"}
          </button>
        </div>
      </div>
      <div className="stack tight">
        <span className="small">Checked and it's innocent? Clear it and let them continue:</span>
        <div className="row">
          <input className="input" maxLength={300} placeholder="Reason, e.g. confirmed dates with the candidate" value={note}
            onChange={(ev) => setNote(ev.target.value)} />
          <button className="btn sm" onClick={clear} disabled={!!busy}>{busy === "clear" ? <Spinner /> : null} Clear &amp; continue</button>
        </div>
      </div>
    </div>
  );
}
