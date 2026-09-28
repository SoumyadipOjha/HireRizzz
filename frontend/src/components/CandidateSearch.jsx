import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { COLUMN, ago, initials, nameOf, navigate } from "../lib.js";
import { Pill, Spinner } from "../ui.jsx";

const TONE = { selected: "ok", rejected: "muted", fraud: "bad", resume_review: "accent", final_review: "accent", interview: "info" };

/** Search every candidate across all jobs; picking one opens their card on that job's board. */
export default function CandidateSearch({ jobs }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(null);
  const [error, setError] = useState(null);
  const [active, setActive] = useState(0);
  const box = useRef(null);
  const input = useRef(null);

  const load = () => {
    setError(null);
    api.overview()
      .then((d) => setAll(Object.values(d?.index?.candidates || {})))
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    const onDoc = (e) => box.current && !box.current.contains(e.target) && setOpen(false);
    const onKey = (e) => {
      if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  const titles = useMemo(() => Object.fromEntries((jobs || []).map((j) => [j.job_id, j.title])), [jobs]);
  const results = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s || !all) return [];
    return all
      .filter((c) => `${nameOf(c)} ${c.source_file || ""} ${titles[c.job_id] || ""}`.toLowerCase().includes(s))
      .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""))
      .slice(0, 8);
  }, [q, all, titles]);

  const pick = (c) => {
    setOpen(false);
    setQ("");
    navigate(`/jobs/${encodeURIComponent(c.job_id)}?c=${c.candidate_id}`);
  };

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") (e.preventDefault(), setActive((a) => Math.min(a + 1, results.length - 1)));
    else if (e.key === "ArrowUp") (e.preventDefault(), setActive((a) => Math.max(a - 1, 0)));
    else if (e.key === "Enter" && results[active]) pick(results[active]);
    else if (e.key === "Escape") (setOpen(false), input.current?.blur());
  };

  return (
    <div className="cand-search" ref={box}>
      <span className="cand-search-icon" aria-hidden>⌕</span>
      <input
        ref={input}
        className="input"
        placeholder="Search candidates in all jobs"
        value={q}
        onFocus={() => (setOpen(true), load())}
        onChange={(e) => (setQ(e.target.value), setOpen(true), setActive(0))}
        onKeyDown={onKeyDown}
        aria-label="Search candidates"
      />
      <kbd className="cand-search-kbd">/</kbd>
      {open && q.trim() && (
        <div className="cand-search-menu card">
          {error ? (
            <div className="faint small pad">{error}</div>
          ) : !all ? (
            <div className="faint small pad row"><Spinner /> Searching…</div>
          ) : results.length === 0 ? (
            <div className="faint small pad">No candidate matches “{q.trim()}”.</div>
          ) : (
            results.map((c, i) => (
              <button
                key={c.candidate_id}
                className={`cand-search-item${i === active ? " on" : ""}`}
                onMouseEnter={() => setActive(i)}
                onClick={() => pick(c)}
              >
                <span className="avatar sm">{initials(nameOf(c))}</span>
                <span className="grow" style={{ minWidth: 0 }}>
                  <span className="ellipsis" style={{ display: "block", fontWeight: 600 }}>{nameOf(c)}</span>
                  <span className="ellipsis faint small" style={{ display: "block" }}>
                    {titles[c.job_id] || c.job_id}{c.updated_at ? ` · ${ago(c.updated_at)}` : ""}
                  </span>
                </span>
                {c.overall_status === "cooling" ? (
                  <Pill tone="warn">Cooling</Pill>
                ) : (
                  <Pill tone={TONE[c.board_column] || ""}>{COLUMN[c.board_column]?.label || c.board_column}</Pill>
                )}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
