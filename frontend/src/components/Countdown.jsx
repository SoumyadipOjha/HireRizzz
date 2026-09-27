import { useEffect, useState } from "react";

/** A deadline as a moment: a bare date ("2026-10-15") means the end of that day, local time. */
export function deadlineTime(deadline) {
  if (!deadline) return null;
  const t = /^\d{4}-\d{2}-\d{2}$/.test(deadline) ? new Date(`${deadline}T23:59:59`) : new Date(deadline);
  return Number.isNaN(t.getTime()) ? null : t;
}

const pad = (n) => String(n).padStart(2, "0");

/** Live countdown to a job's deadline. `compact` = one short line for lists. */
export default function Countdown({ deadline, compact = false }) {
  const end = deadlineTime(deadline);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!end) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [deadline]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!end) return compact ? null : <span className="countdown none">No deadline</span>;
  const left = end.getTime() - now;
  const date = end.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  if (left <= 0) {
    return <span className="countdown over" title={`Deadline was ${date}`}><span className="cd-dot" />Deadline passed · {date}</span>;
  }
  const s = Math.floor(left / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const urgent = left < 3 * 86400 * 1000;
  return (
    <span className={`countdown${urgent ? " urgent" : ""}`} title={`Applications close ${date}`}>
      <span className="cd-dot" />
      {compact ? "Closes in " : "Applications close in "}
      <b>{d > 0 ? `${d}d ` : ""}{pad(h)}h {pad(m)}m {pad(sec)}s</b>
      {!compact && <span className="cd-date"> · {date}</span>}
    </span>
  );
}
