import { useEffect } from "react";
import { TONE, fmtTime, label, toast, useToasts } from "./lib.js";

export function Pill({ value, tone, children, dot = false }) {
  const t = tone || TONE[value] || "";
  return (
    <span className={`pill ${t}`}>
      {dot && <span className="dot" />}
      {children ?? label(value)}
    </span>
  );
}

export function Modal({ title, onClose, children, footer, wide = false, extra }) {
  useEscape(onClose);
  return (
    <div className="modal-wrap" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          {extra}
          <button className="btn ghost icon" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function useEscape(fn) {
  useEffect(() => {
    const h = (e) => e.key === "Escape" && !e.defaultPrevented && fn();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [fn]);
}

export function Section({ title, right, children }) {
  return (
    <section className="section">
      <div className="section-title">
        <span className="grow">{title}</span>
        {right}
      </div>
      {children}
    </section>
  );
}

export function KV({ rows }) {
  const shown = rows.filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false);
  if (!shown.length) return null;
  return (
    <dl className="kv">
      {shown.map(([k, v]) => (
        <FragmentKV key={k} k={k} v={v} />
      ))}
    </dl>
  );
}
function FragmentKV({ k, v }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </>
  );
}

export function Toasts() {
  const items = useToasts();
  return (
    <div className="toasts" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>{t.text}</div>
      ))}
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

export function CopyButton({ text, label: text2 = "Copy" }) {
  return (
    <button
      className="btn sm"
      onClick={() =>
        navigator.clipboard.writeText(text).then(
          () => toast("Copied"),
          () => toast("Couldn't copy", "error"),
        )
      }
    >
      {text2}
    </button>
  );
}

export const yesNo = (v) => (v === true ? "Yes" : v === false ? "No" : v == null ? null : String(v));

export function EmailLine({ n }) {
  return (
    <div className="row wrap">
      <span className="muted">Email “{label(n.kind)}”:</span>
      <Pill value={n.status} />
      {n.to && <span className="muted">to {n.to}</span>}
      {n.at && <span className="faint">{fmtTime(n.at)}</span>}
      {n.error && <span style={{ color: "var(--bad)" }}>{n.error}</span>}
    </div>
  );
}
