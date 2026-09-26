# HireRizz frontend

React + Vite app for recruiters and hiring managers, plus the candidate's interview page.

| Page | URL | Who |
|---|---|---|
| Jobs | `/` | Every posted job, with its funnel. **Post a job** drafts the JD with AI. |
| Job board | `/jobs/<job_id>` | The job's candidates in columns (Applied → Resume review → Interview → Final review → Selected / Rejected / Fraud stopped). Click a card for the side panel: overview and decisions, resume, interview, credibility. `?c=<candidate_id>` opens a candidate. |
| Interview | `/interview/<token>` | Candidate screening call, voice or text (`public/interview.html`, plain HTML) |

Everything calls the backend API at the address in `config.js` (`window.HIRERIZZ_API`).

```
src/
  pages/        JobsPage (list), BoardPage (columns)
  components/   CandidateDrawer + its tabs, PostJobModal (JD writer), ResultsModal, JobDetailsModal, SystemModal
  api.js        every backend call
  lib.js        board columns, labels, polling, "Approving as", toasts, routing
public/         interview.html, config.js, favicon.svg (copied to dist/ as they are)
```

## Local

```
npm install
npm run build      # dist/ -> served by `uv run screening serve` (backend/) on http://127.0.0.1:8765
npm run dev        # or: live-reloading dev server on http://localhost:5173, API proxied to :8765
```

## Deploy on Vercel

1. Vercel -> **Add New Project** -> import the GitHub repo -> **Root Directory: `frontend`**.
2. Environment variable **`HIRERIZZ_API_URL`** = your backend URL, e.g. `https://hirerizz-backend.onrender.com`.
3. Deploy. `vercel.json` runs `npm run build` (Vite, then `build.mjs` writes `dist/config.js`) and routes
   `/jobs/...` to the app and `/interview/<token>` to the interview page.
4. Put the Vercel URL (e.g. `https://hirerizz.vercel.app`) in the backend's `CORS_ORIGINS` and `PUBLIC_BASE_URL`.
