# HireRizz

*Your recruiter's got rizz now.* AI resume screening, a live AI voice screening call, evidence-backed
scoring, and approval gates where people make the final call.

```
resume upload -> AI reads + scores resume -> recruiter approves -> invite email (QR + link)
   -> AI voice/text screening call -> AI scores the interview (quotes checked against the transcript)
   -> hiring manager approves / overrides -> selection or rejection email
```

| Folder | What | Deploy |
|---|---|---|
| [`backend/`](backend/) | Python API: Gemini stages, MongoDB, Gmail SMTP, approvals ([README](backend/README.md)) | Render (`render.yaml`) |
| [`frontend/`](frontend/) | React + Vite app: jobs, job boards, candidate side panel; plus the candidate interview page ([README](frontend/README.md)) | Vercel (`frontend/vercel.json`) |

## Run locally

```bash
cd backend
uv sync
copy .env.example .env      # fill in GEMINI_API_KEY, SMTP_USER, SMTP_PASS
uv run screening serve      # API + interview pages (+ the app once built: cd ../frontend && npm install && npm run build)
uv run pytest
```

## Deploy

1. **MongoDB Atlas** (free cluster): create a database user and allow access from anywhere (0.0.0.0/0);
   copy the `mongodb+srv://...` connection string.
2. **Backend on Render**: New -> Blueprint -> this repo. Fill in the secrets it asks for
   (`GEMINI_API_KEY`, `MONGODB_URI`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM_ADDRESS`; leave `CORS_ORIGINS`
   and `PUBLIC_BASE_URL` for step 4). Note the URL, e.g. `https://hirerizz-backend.onrender.com`.
3. **Frontend on Vercel**: Add New Project -> this repo -> Root Directory `frontend` ->
   environment variable `HIRERIZZ_API_URL` = the Render URL. Note the URL, e.g. `https://hirerizz.vercel.app`.
4. Back on Render, set `CORS_ORIGINS` and `PUBLIC_BASE_URL` to the Vercel URL and redeploy.

The dashboard needs a **sign-in**: one account, defaults in `backend/src/screening/dashboard/auth.py`.
Set `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` on Render to change them (that signs everyone out).
Every decision is recorded under the signed-in account. Candidates' interview links need no login.
