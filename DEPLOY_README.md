# Deploying Dalil

Dalil is live at **[https://dalil.online](https://dalil.online)**, served by a
Render web service built from this repo's `Dockerfile`. A push to `main` is
what deploys it. See [README.md](README.md) for the app itself, its
configuration and its logging.

## Deploy files: active and legacy

| File | Status | Purpose |
|---|---|---|
| [`deploy.ps1`](deploy.ps1) | **Active** | The way to deploy. Checks the tree, pushes `main`, waits for Render to report the commit live, then smoke-tests it. |
| [`.render.env.example`](.render.env.example) | **Active** | Template for `.render.env`, the local, gitignored file that gives `deploy.ps1` a read-only Render API key. |
| [`Dockerfile`](Dockerfile) | **Active** | What Render builds. Listens on `$PORT`, with 7860 as the fallback. |
| [`.dockerignore`](.dockerignore) | **Active** | What stays out of the image. |
| [`tests/test_repo_hygiene.py`](tests/test_repo_hygiene.py) | **Active** | Guards against filenames that cannot be checked out on Linux, which broke a Render deploy on 23 Aug 2026. |
| [`scripts/deploy_cloudrun.sh`](scripts/deploy_cloudrun.sh) | Legacy | Google Cloud Run fallback, kept in case Render is unavailable. Not used by the live site. |
| [`.gcloudignore`](.gcloudignore) | Legacy | Upload filter for the Cloud Run script only. |
| YAML front matter in [`README.md`](README.md) | Legacy | Configured a Hugging Face Docker Space, an earlier host. Render ignores it. |

## How deployment works

Render is connected to this repo's GitHub remote (`ibmallawati/Dalil`, branch
`main`) with auto-deploy on push. That connection lives in the Render
dashboard; there is no `render.yaml`. So the actual deploy trigger is:

```powershell
git push origin main
```

`deploy.ps1` wraps that one command. It refuses to push a dirty tree or a
syntax error. Then, if a Render API key is set up, it polls Render's API until
the new commit is live and smoke-tests the service.

The API key is used **read-only**, to look up deploy status and the service's
URL. It never triggers a deploy; `git push` does.

## One-time setup

1. **Set the environment variables on the Render service.** Render → the
   service → **Environment**:

   | Name | Required | Notes |
   |---|---|---|
   | `LLM_API_KEY` | Yes | The app exits at startup without it. |
   | `LLM_MODEL` | No | Comma-separated failover chain, e.g. `gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite`. |
   | `LLM_BASE_URL`, `EMBED_BACKEND`, `EMBED_MODEL`, `EMBED_DIM`, `LOG_LEVEL` | No | Defaults in `src/chatbot/config.py`. Keep `EMBED_BACKEND` at its default (`gemini`); `local` needs about 1.2 GB of memory. |

   Do not set `PORT`. Render provides it.

2. **Set the health check path** to `/healthz` (Render → service →
   **Settings**). Render then waits for the app to answer before switching
   traffic to a new deploy.

3. **Custom domain.** `dalil.online` is attached under Render → service →
   **Settings → Custom Domains**. Render handles its TLS certificate.

4. **Optional: a Render API key for `deploy.ps1`.** Without it the script
   still pushes, but it cannot tell you whether the deploy succeeded.

   - Create a key: https://dashboard.render.com/u/settings#api-keys
   - Create the local config (gitignored, never commit it):

     ```powershell
     Copy-Item .render.env.example .render.env
     notepad .render.env   # paste your real API key
     ```

     ```
     RENDER_API_KEY=rnd_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
     RENDER_SERVICE_ID=srv-d9abdncs728c73eg8qeg
     ```

   - Check that `RENDER_SERVICE_ID` is the service serving `dalil.online`.
     It's the `srv-…` part of the service's dashboard URL. The value above
     is the script's built-in default.

## Deploying

From the repo root:

```powershell
git add <files>
git commit -m "..."
.\deploy.ps1
```

The script:

1. Confirms you are on `main`, the working tree is clean, and every tracked
   `.py` file compiles.
2. Runs `git push origin main`.
3. Polls Render until the new commit's deploy reaches `live` (up to 10
   minutes by default).
4. Calls the service's `/healthz`, `/` and `/chat` to confirm it is
   answering.

"Clean" includes **untracked files**. A stray file such as an unsaved report
in `docs/` stops the preflight. Commit it, delete it, or run with `-Force`
(the push still deploys only what is committed).

Before pushing, run the test suite. At minimum, run the check that protects
the Linux checkout:

```powershell
pytest tests/test_repo_hygiene.py
```

### Flags

| Flag | What it does |
|---|---|
| `-Force` | Push even with uncommitted changes. Deploys the last commit as-is; uncommitted work is not included. |
| `-DryRun` | Run every check, then stop before the push. Validates the script and `.render.env` without deploying. |
| `-SkipSmokeTest` | Push and confirm the deploy is live, but skip the endpoint checks. The `/chat` check sends one real question, which may use one LLM request and counts against the 20-per-minute limit. |
| `-TimeoutSeconds <n>` | How long to wait for `live` before giving up. Default 600. |

The smoke test runs against the service's `onrender.com` URL, which the
Render API returns. To check the public domain as well:

```powershell
curl.exe -s https://dalil.online/healthz
```

## Manual fallback (no script)

These commands are for Windows PowerShell. They use `curl.exe`, not
`curl`: in Windows PowerShell 5.1, `curl` is an alias for
`Invoke-WebRequest`, which rejects curl's flags.

```powershell
# Deploy
git push origin main

# Latest deploy's status. Needs your Render API key in this session:
#   $env:RENDER_API_KEY = "rnd_..."
curl.exe -s -H "Authorization: Bearer $env:RENDER_API_KEY" `
     "https://api.render.com/v1/services/srv-d9abdncs728c73eg8qeg/deploys?limit=1"

# Smoke-test once it's live
curl.exe -s https://dalil.online/healthz
```

To send a test question, put it in a UTF-8 file and send the file. Arabic
typed directly into a `curl.exe` argument can reach the server as `?`
characters, and the question is then refused as out of scope.

```powershell
# WriteAllText saves UTF-8 without a BOM. The file goes in %TEMP%, because an
# untracked file in the repo would stop deploy.ps1's preflight.
$q = "$env:TEMP\dalil-question.json"
[IO.File]::WriteAllText($q, '{"history":[{"role":"user","content":"كم رسوم تجديد جواز السفر؟"}]}')

curl.exe -s -X POST https://dalil.online/chat `
     -H "Content-Type: application/json; charset=utf-8" `
     --data-binary "@$q"
```

A working deploy returns `"format":"answer"` and a `"served_by"` model. The
reply text arrives as `\u` escapes, which is normal. A refusal with
`"reason":"no_grounding"` for this question usually means the Arabic was
garbled on the way in.

On macOS or Linux, use plain `curl` with the same flags, and write the file
with any UTF-8 editor or `printf`.

Or push, then watch the deploy turn green in the dashboard:
`https://dashboard.render.com/web/srv-d9abdncs728c73eg8qeg`.

## Rollback

Render keeps every previous deploy. Fastest path: dashboard → **Events** →
pick a previous successful deploy → **Rollback to this deploy**.

To roll back through git instead, so the rollback is itself a normal deploy:

```powershell
git revert <bad-commit-sha>
.\deploy.ps1
```

## Troubleshooting

- **Build failed.** Check the build output in Render's **Logs** tab. It's
  usually a `requirements.txt` install error.
- **`git clone` fails with `File name too long`.** A tracked filename is over
  Linux's 255-byte limit. Arabic is two bytes per letter, so a name that is
  fine on Windows can be too long on Render. `tests/test_repo_hygiene.py`
  catches this; shorten or untrack the file.
- **Service starts, then exits with `LLM_API_KEY is missing`.** The variable
  is not set on the Render service (setup step 1).
- **Unhealthy or crashing after deploy.** Every request logs JSON lines to
  stdout (`chat_request_received`, `stage_completed`, `chat_request_failed`)
  with a `request_id` to filter on. See the Logging section of
  [README.md](README.md). For more detail, set `LOG_LEVEL=DEBUG` on the
  service, then set it back to `INFO`.
- **`deploy.ps1` times out waiting for `live`.** Docker builds can be slow on
  Render. Re-run with a larger `-TimeoutSeconds`, or watch the dashboard; the
  push already happened.
- **Users see "عدد كبير من الطلبات".** That's the rate limit: 20 `/chat`
  requests per minute and 100 requests per hour per IP.

## Legacy targets

### Google Cloud Run (`scripts/deploy_cloudrun.sh`)

A fallback host, not used by `dalil.online`. It needs `gcloud`, a project,
and an `LLM_API_KEY` secret in Secret Manager. The script checks all three
before building:

```bash
bash scripts/deploy_cloudrun.sh
```

It deploys to `me-central1` with 4 GiB of memory. That size and its "baking
in the 1.1 GB embedding model" message come from when embeddings ran
in-process. Both are larger than the current image needs.

### Hugging Face Space

The YAML front matter at the top of `README.md` (`sdk: docker`,
`app_port: 7860`) configured a Docker Space. It is harmless to keep and is not
part of the Render deploy.

## Stale text left in the Cloud Run script

This comment predates the Gemini embeddings. It doesn't change what the
script does and is left as-is:

- `scripts/deploy_cloudrun.sh` describes a 1.1 GB embedding model that the
  image no longer contains.
