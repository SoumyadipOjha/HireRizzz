// Talks to the HireRizz backend. window.HIRERIZZ_API comes from /config.js ("" = same address).
const BASE = String(window.HIRERIZZ_API || "").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

// ------------------------------------------------------------ session (the login token, kept in this browser)
const TOKEN_KEY = "hirerizz_token";
const USER_KEY = "hirerizz_user";
const sessionListeners = new Set();
const read = (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
let session = { token: read(TOKEN_KEY), user: read(USER_KEY) };
function setSession(token, user) {
  session = { token: token || "", user: user || "" };
  try {
    if (token) { localStorage.setItem(TOKEN_KEY, token); localStorage.setItem(USER_KEY, user); }
    else { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); }
  } catch { /* private mode: the session lasts until the tab closes */ }
  sessionListeners.forEach((l) => l());
}
export const getSession = () => session;
export const onSession = (l) => (sessionListeners.add(l), () => sessionListeners.delete(l));
export function signOut() { setSession("", ""); }

async function send(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  try {
    return await fetch(BASE + path, { method, cache: "no-store", headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError("Can't reach the HireRizz server. Is it running?", 0);
  }
}

async function request(path, opts = {}) {
  const r = await send(path, opts);
  if (r.status === 401 && path !== "/api/login") signOut();  // expired or missing: back to the login
  const text = await r.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* not JSON */
  }
  if (!r.ok) throw new ApiError((data && data.error) || `${r.status} ${r.statusText}`, r.status);
  return data;
}

const q = (jobId) => (jobId ? `?job=${encodeURIComponent(jobId)}` : "");

export const api = {
  jobs: () => request("/api/jobs"),
  job: (id) => request(`/api/jobs/${encodeURIComponent(id)}`),
  setJobStatus: (id, status) => request(`/api/jobs/${encodeURIComponent(id)}/status`, { method: "POST", body: { status } }),
  overview: (jobId) => request(`/api/overview${q(jobId)}`),
  candidate: (cid) => request(`/api/candidate/${cid}`),
  results: (jobId) => request(`/api/results${q(jobId)}`),
  downloadResultsCsv: async (jobId) => {
    const r = await send(`/api/results.csv${q(jobId)}`);
    if (r.status === 401) signOut();
    if (!r.ok) throw new ApiError(`${r.status} ${r.statusText}`, r.status);
    const name = (r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || "results.csv";
    const url = URL.createObjectURL(await r.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  login: async (username, password) => {
    const r = await request("/api/login", { method: "POST", body: { username, password } });
    setSession(r.token, r.user);
    return r;
  },
  me: () => request("/api/me"),
  failures: () => request("/api/failures"),
  approve: (gate, decisions, by, note) =>
    request(`/api/approve/${gate}`, { method: "POST", body: { decisions, by, note: note || undefined } }),
  sendResults: (jobId) => request("/api/send-results", { method: "POST", body: { job_id: jobId } }),
  uploadResumes: (jobId, files) => request("/api/resumes", { method: "POST", body: { job_id: jobId, files } }),
  uploadLinkedin: (cid, name, data) => request(`/api/candidate/${cid}/linkedin`, { method: "POST", body: { name, data } }),
  clearFraud: (cid, by, note) => request(`/api/candidate/${cid}/clear-fraud`, { method: "POST", body: { by, note } }),
  requestClarification: (cid) => request(`/api/candidate/${cid}/request-clarification`, { method: "POST", body: {} }),
  jdDraft: () => request("/api/jd/draft"),
  writeJd: (brief, company_name) => request("/api/jd/draft", { method: "POST", body: { brief, company_name } }),
  postJob: (job, questions, by, jobId) =>
    request("/api/jobs", { method: "POST", body: { job, questions, by, job_id: jobId || undefined } }),
};

export function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
