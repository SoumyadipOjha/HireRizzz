import { useMemo, useState } from "react";
import { toast } from "../lib.js";
import { Modal } from "../ui.jsx";

const LIMIT = 3000; // LinkedIn post length limit

const tag = (s) => "#" + String(s).replace(/[^A-Za-z0-9]+(.)?/g, (_, c) => (c ? c.toUpperCase() : "")).replace(/^./, (c) => c.toUpperCase());

/** A LinkedIn-ready post (plain text: LinkedIn doesn't render markdown) from a job. */
export function linkedinPost(job) {
  const j = job || {};
  const exp = j.min_experience_years != null || j.max_experience_years != null
    ? `${j.min_experience_years ?? 0}${j.max_experience_years != null ? `–${j.max_experience_years}` : "+"} years`
    : null;
  const city = (j.location || "").split(/[,(]/)[0].trim();
  const desc = String(j.description || "").trim();
  const paras = desc.split(/\n\s*\n/).map((p) => p.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean);
  const bulletish = desc.split("\n").map((l) => l.trim()).filter((l) => /^[-*•]\s+/.test(l)).map((l) => l.replace(/^[-*•]\s+/, ""));
  const intro = paras.find((p) => !/^[-*•]/.test(p)) || "";

  const lines = [];
  lines.push(`🚀 We're hiring: ${j.title}${j.company_name ? ` at ${j.company_name}` : ""}!`);
  lines.push("");
  if (intro) lines.push(intro.length > 600 ? `${intro.slice(0, 597).trimEnd()}…` : intro, "");
  const meta = [j.location && `📍 Location: ${j.location}`, exp && `💼 Experience: ${exp}`].filter(Boolean);
  if (meta.length) lines.push(...meta, "");
  if (bulletish.length) {
    lines.push("🛠️ What you'll do:");
    bulletish.slice(0, 6).forEach((b) => lines.push(`• ${b}`));
    lines.push("");
  }
  if (j.must_have_skills?.length) {
    lines.push("✅ Must-have skills:");
    j.must_have_skills.forEach((s) => lines.push(`• ${s}`));
    lines.push("");
  }
  if (j.nice_to_have_skills?.length) {
    lines.push(`⭐ Nice to have: ${j.nice_to_have_skills.join(", ")}`, "");
  }
  lines.push("🤖 Our process is quick: after you apply, shortlisted candidates get a short AI screening call they can take any time, from any device.");
  lines.push("");
  lines.push(j.apply_url ? `👉 Apply here: ${j.apply_url}` : "👉 Interested? Apply through our careers page, or comment / DM me and I'll share the link.");
  lines.push("");
  const tags = [
    "#hiring", "#jobs", tag(j.title || "job"),
    ...(j.must_have_skills || []).slice(0, 3).map(tag),
    city && tag(`${city} jobs`), j.company_name && tag(j.company_name),
  ].filter(Boolean);
  lines.push([...new Set(tags)].join(" "));
  return lines.join("\n");
}

export default function LinkedInModal({ job, onClose, justPosted = false }) {
  const initial = useMemo(() => linkedinPost(job), [job]);
  const [text, setText] = useState(initial);

  const copy = () =>
    navigator.clipboard.writeText(text).then(
      () => toast("Copied. Paste it into a new LinkedIn post.", "ok"),
      () => toast("Couldn't copy: select the text and copy it by hand.", "error"),
    );
  const open = () => {
    copy();
    window.open("https://www.linkedin.com/feed/?shareActive=true", "_blank", "noopener");
  };

  return (
    <Modal
      title={justPosted ? "🎉 Job posted! Share it on LinkedIn" : "LinkedIn post"}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={() => setText(initial)} disabled={text === initial}>Reset</button>
          <span className="grow" />
          <button className="btn" onClick={copy}>Copy text</button>
          <button className="btn primary shine" onClick={open}>Copy &amp; open LinkedIn ↗</button>
        </>
      }
    >
      <p className="muted small">
        Ready to paste: plain text with emoji, bullets and hashtags (LinkedIn doesn't support bold or markdown). Edit
        anything below first if you like.
      </p>
      <textarea className="textarea li-text" rows={18} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="row between small">
        <span className="faint">{job?.apply_url ? "Includes your job posting link." : "Tip: add a public job posting link in Edit job to include an Apply link."}</span>
        <span className={text.length > LIMIT ? "" : "faint"} style={text.length > LIMIT ? { color: "var(--bad)" } : undefined}>
          {text.length}/{LIMIT}
        </span>
      </div>
    </Modal>
  );
}
