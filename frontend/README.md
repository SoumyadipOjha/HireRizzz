# HireRizz frontend

Two static pages, no framework:

| Page | URL | Who |
|---|---|---|
| `index.html` | `/` | Recruiter / hiring manager dashboard |
| `interview.html` | `/interview/<token>` | Candidate screening call (voice or text) |

Both call the backend API at the address in `config.js` (`window.HIRERIZZ_API`).

## Local

Nothing to do: `uv run screening serve` in `backend/` serves these pages and the API from
http://127.0.0.1:8765 (`config.js` stays empty = same address).

## Deploy on Vercel

1. Vercel -> **Add New Project** -> import the GitHub repo -> **Root Directory: `frontend`**.
2. Environment variable **`HIRERIZZ_API_URL`** = your backend URL, e.g. `https://hirerizz-backend.onrender.com`.
3. Deploy. `vercel.json` runs `node build.mjs` (writes `dist/config.js`) and routes `/interview/<token>`.
4. Put the Vercel URL (e.g. `https://hirerizz.vercel.app`) in the backend's `CORS_ORIGINS` and `PUBLIC_BASE_URL`.
