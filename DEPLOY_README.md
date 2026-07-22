# Deploying Chatbot-Public

How this app actually gets to Render, and every command involved. See
[README.md.txt](README.md.txt) for the app itself and its logging setup.

## How deployment works here

Render is connected to this repo's GitHub remote (`ibmallawati/chatbot-public`,
branch `main`) with auto-deploy on push enabled. That connection lives in the
Render dashboard, not in this repo — there's no `render.yaml`. So the actual
deploy trigger is one command:

```powershell
git push origin main
```

Everything in [deploy.ps1](deploy.ps1) exists to make that safe and
verifiable: it refuses to push broken or uncommitted code, then (if you've
set up a Render API key — see below) polls Render's API to confirm the new
commit actually went live, and smoke-tests the running service.

The API key is used **read-only** — to look up deploy status and the
service's public URL. It never triggers the deploy itself; `git push` does.

## One-time setup

1. **Get a Render API key**: https://dashboard.render.com/u/settings#api-keys
   -> "Create API Key".
2. **Create your local config** (already gitignored, never commit it):

   ```powershell
   Copy-Item .render.env.example .render.env
   notepad .render.env   # paste your real API key
   ```

   `.render.env` contents:
   ```
   RENDER_API_KEY=rnd_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
   RENDER_SERVICE_ID=srv-d9abdncs728c73eg8qeg
   ```

   This step is optional — `deploy.ps1` still pushes without it — but
   without a key it can't tell you whether the deploy succeeded.

3. **(Recommended, one-time, in the Render dashboard)** Set the service's
   *Health Check Path* to `/healthz` under Settings. Render uses this to
   know when a new instance is actually ready during a deploy, instead of
   just "the process started."

## Deploying

```powershell
cd Chatbot-Public
.\deploy.ps1
```

This will:
1. Confirm you're on `main` with a clean working tree and that every
   tracked `.py` file compiles.
2. `git push origin main`.
3. Poll Render until the new commit's deploy reaches `live` (up to 10 min
   by default).
4. Hit the live service's `/healthz`, `/`, and `/chat` to confirm it's
   actually answering, not just "deployed."

### Flags

| Flag | What it does |
|---|---|
| `-Force` | Push even with uncommitted local changes (deploys the last commit as-is; uncommitted work is NOT included). |
| `-DryRun` | Run every check but skip the push and everything after it — validates the script/config without deploying. |
| `-SkipSmokeTest` | Push and confirm it's live, but skip the `/chat` smoke test (that call costs one real Hugging Face inference request and shows up in `chat_log.csv`). |
| `-TimeoutSeconds <n>` | How long to wait for "live" before giving up (default 600). |

### Normal workflow

```powershell
git add <files>
git commit -m "..."
.\deploy.ps1
```

If `deploy.ps1` reports a preflight failure (dirty tree, syntax error, wrong
branch), fix that and re-run — nothing is pushed until preflight passes.

## Manual fallback (no script)

If you just want the raw commands:

```powershell
# Deploy
git push origin main

# Check the latest deploy's status (requires RENDER_API_KEY)
curl -H "Authorization: Bearer $env:RENDER_API_KEY" `
     "https://api.render.com/v1/services/srv-d9abdncs728c73eg8qeg/deploys?limit=1"

# Smoke-test once it's live
curl https://<your-service>.onrender.com/healthz
curl -X POST https://<your-service>.onrender.com/chat `
     -H "Content-Type: application/json" `
     -d '{"history":[{"role":"user","content":"hello"}]}'
```

Or skip the CLI entirely: push, then watch
`https://dashboard.render.com/web/srv-d9abdncs728c73eg8qeg` for the deploy to
turn green.

## Rollback

Render keeps every previous deploy. Fastest path: dashboard -> **Events** tab
-> pick a prior successful deploy -> **Rollback to this deploy**. To roll
back via git instead (so the rollback is itself deployable/repeatable):

```powershell
git revert <bad-commit-sha>
.\deploy.ps1
```

## Troubleshooting a bad deploy

- **Build failed**: check the Render **Logs** tab during the build phase —
  usually a `requirements.txt` install error.
- **Build succeeded, service unhealthy / crashing**: check **Logs** after
  deploy — every request now logs structured JSON (`chat_request_received`,
  `stage_completed`, `chat_request_failed`, ...) with a `request_id` you can
  filter on. See [README.md.txt](README.md.txt)'s Logging & Observability
  section for the full field reference. Temporarily set `LOG_LEVEL=DEBUG`
  in the Render dashboard's environment variables for more detail (e.g.
  per-chunk RAG retrieval scores), then set it back to `INFO`.
- **`deploy.ps1` times out waiting for "live"**: Docker builds can be slow
  on Render's free tier. Re-run with a larger `-TimeoutSeconds`, or just
  watch the dashboard — the push already happened.
