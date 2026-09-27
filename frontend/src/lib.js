import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { getSession, onSession } from "./api.js";

// ---------------------------------------------------------------- board

export const COLUMNS = [
  { key: "applied", label: "Applied", hint: "Resume being read and scored" },
  { key: "resume_review", label: "Resume review", hint: "AI has scored it: a recruiter decides" },
  { key: "interview", label: "Screening", hint: "Invited to the AI screening call" },
  { key: "final_review", label: "Final review", hint: "Screened and scored: the manager decides" },
  { key: "selected", label: "Shortlisted", hint: "On the final shortlist" },
  { key: "rejected", label: "Rejected", hint: "Not moving forward" },
  { key: "fraud", label: "Fraud stopped", hint: "Stopped by the resume checks" },
];
export const COLUMN = Object.fromEntries(COLUMNS.map((c) => [c.key, c]));

export const STAGES = [
  ["stage1_extraction", "Resume read"],
  ["stage2_shortlisting", "Resume scored"],
  ["stage3_calling", "Screening call"],
  ["stage4_evaluation", "Screening scored"],
];

export const TONE = {
  success: "ok", completed: "ok", shortlisted: "ok", selected: "ok", sent: "ok",
  failed: "bad", rejected: "bad", not_selected: "bad", fraud: "bad",
  awaiting: "warn", skipped: "muted", outbox: "muted", pending: "info", active: "info", evaluated: "accent",
  open: "ok", closed: "muted",
};

export const LABEL = {
  shortlisted: "Shortlisted", rejected: "Rejected", success: "Done", failed: "Failed", awaiting: "Waiting",
  skipped: "Skipped", pending: "Not started", running: "Running", sent: "Sent", outbox: "Outbox",
  selected: "Shortlisted", not_selected: "Not shortlisted", open: "Open", closed: "Closed",
  positive: "Positive", neutral: "Neutral", negative: "Negative", mixed: "Mixed",
  completed: "Completed", abandoned: "Interrupted", cooling: "Cooling period", call_interrupted: "call interrupted", opted_out: "Opted out", rescheduled: "Rescheduled",
};
export const label = (v) => (v == null ? "—" : LABEL[v] || String(v).replace(/_/g, " "));

// ---------------------------------------------------------------- candidates

export const nameOf = (e) => e.display_name || basename(e.source_file) || "Unnamed";
export const basename = (p) => (p ? String(p).split(/[\\/]/).pop() : "");
export const resumeDecision = (e) => e.reviews?.shortlist?.decision || e.stages?.stage2_shortlisting?.decision || null;
export const waitingShortlist = (e) =>
  !e.fraud_blocked && e.stages?.stage2_shortlisting?.status === "success" && !e.reviews?.shortlist;
export const waitingFinal = (e) =>
  !e.fraud_blocked && e.stages?.stage4_evaluation?.status === "success" && !e.reviews?.final &&
  resumeDecision(e) === "shortlisted";
export const hasFailure = (e) => Object.values(e.stages || {}).some((s) => s.status === "failed");
export const initials = (name) =>
  name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";

export function credibilityFlag(e) {
  if (e.fraud_blocked) return { tone: "bad", text: "Fraud detected" };
  if (e.fraud_cleared) return { tone: "muted", text: `Flags cleared by ${e.fraud_cleared.by}` };
  if (e.credibility?.red) return { tone: "bad", text: `${e.credibility.red} credibility issue${e.credibility.red > 1 ? "s" : ""}` };
  if (e.credibility?.amber) return { tone: "warn", text: `${e.credibility.amber} flag${e.credibility.amber === 1 ? "" : "s"}` };
  return null;
}

// ---------------------------------------------------------------- formatting

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}
export function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(s)) return "";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return fmtDate(iso);
}
export const num = (v, d = 0) => (v == null || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(d));
export function duration(sec) {
  if (sec == null) return null;
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m ? `${m}m ${s}s` : `${s}s`;
}
export const CHANNEL = {
  browser_voice: "Browser voice call", browser_text: "Browser text chat",
  terminal_simulation: "Terminal simulation", local_transcript_file: "Parsed from a local transcript file",
};
export function callLine(call) {
  if (!call) return "";
  return [
    CHANNEL[call.channel] || call.channel,
    call.status && `outcome: ${label(call.status)}`,
    duration(call.duration_seconds),
    call.candidate_turns != null && `${call.candidate_turns} candidate turns`,
    call.agent_llm && `agent: ${call.agent_llm}`,
  ].filter(Boolean).join(" · ");
}

// ---------------------------------------------------------------- hooks

/** Poll `fn` every `ms` (paused while the tab is hidden). Returns {data, error, loading, reload}. */
export function usePoll(fn, ms, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const inflight = useRef(false);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      const data = await fnRef.current();
      setState({ data, error: null, loading: false });
    } catch (error) {
      setState((s) => ({ ...s, error, loading: false }));
    } finally {
      inflight.current = false;
    }
  }, []);
  useEffect(() => {
    setState({ data: null, error: null, loading: true });
    reload();
    if (!ms) return undefined;
    const t = setInterval(() => document.visibilityState === "visible" && reload(), ms);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { ...state, reload };
}

// The signed-in user: every decision is recorded under their name.
export function useSessionUser() {
  return useSyncExternalStore(onSession, () => getSession().user);
}

// ---------------------------------------------------------------- toasts

const toastListeners = new Set();
let toasts = [];
let toastId = 0;
export function toast(text, kind = "info") {
  const id = ++toastId;
  toasts = [...toasts, { id, text, kind }];
  toastListeners.forEach((l) => l());
  setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    toastListeners.forEach((l) => l());
  }, kind === "error" ? 7000 : 4000);
}
export function useToasts() {
  return useSyncExternalStore((l) => (toastListeners.add(l), () => toastListeners.delete(l)), () => toasts);
}

/** Who is deciding (the signed-in account; the server records it the same way). */
export function requireApprover() {
  return getSession().user || "you";
}

// ---------------------------------------------------------------- routing (tiny, history API)

const routeListeners = new Set();
window.addEventListener("popstate", () => routeListeners.forEach((l) => l()));
export function navigate(path) {
  if (path === window.location.pathname + window.location.search) return;
  window.history.pushState(null, "", path);
  routeListeners.forEach((l) => l());
}
export function useLocation() {
  return useSyncExternalStore(
    (l) => (routeListeners.add(l), () => routeListeners.delete(l)),
    () => window.location.pathname + window.location.search,
  );
}
