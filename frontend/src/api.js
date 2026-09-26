// Talks to the HireRizz backend. window.HIRERIZZ_API comes from /config.js ("" = same address).
const BASE = String(window.HIRERIZZ_API || "").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = "GET", body } = {}) {
  let r;
  try {
    r = await fetch(BASE + path, {
      method,
      cache: "no-store",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Can't reach the HireRizz server. Is it running?", 0);
  }
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
  resultsCsvUrl: (jobId) => `${BASE}/api/results.csv${q(jobId)}`,
  failures: () => request("/api/failures"),
  log: (lines = 300) => request(`/api/log?lines=${lines}`),
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
