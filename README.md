---
title Omantel Chatbot
emoji 💬
colorFrom blue
colorTo green
sdk docker
app_port 7860
pinned false
---

# Omantel Chatbot

Flask backend that routes each question to either text-to-SQL (billing/plan
facts) or RAG (policy/how-to questions from PDFs), with a fault-tolerant
per-stage runner so a broken request always tells you which stage broke.

## Project structure

```
src/chatbot/
  app.py                 # Flask app + routes (entry point: python -m chatbot.app)
  config.py              # env vars read/validated in one place -> `settings`
  logging_config.py       # JSON stdout logging setup
  rag/retrieval.py        # embeds + retrieves chunks (LOCAL sentence-transformers)
  rag/arabic.py            # D3 Arabic normalization, shared by index + query paths
  rag/ocr.py                # OCR fallback for scanned PDFs (build-time only)
  sql/engine.py             # single-DB text-to-SQL: schema, guard, execute
  sql/multi_db.py            # ATTACHes multiple SQLite files under one connection
  reliability/               # retry/backoff fault-tolerance layer
    stage_runner.py            # runs a stage, retries per policy, reports outcome
    failure_classifier.py       # decides retry-vs-terminal for an exception
    run_record.py                 # collects one request's stage outcomes, writes the log
scripts/build_index.py     # offline: PDFs -> data/index/{embeddings.npy,chunks.json}
tests/                      # pytest suite
data/
  source_docs/               # source PDFs
  index/                       # prebuilt embeddings + chunk metadata (committed)
  databases/                     # SQLite files the text-to-SQL path can query
static/, templates/           # Flask UI assets
logs/                           # gitignored; chat_log.csv + stage_log.jsonl land here
```

## Deployment

- **Host**: [Render](https://dashboard.render.com), web service `srv-d9abdncs728c73eg8qeg`,
  built directly from this repo's [Dockerfile](Dockerfile) (no `render.yaml` —
  build/start command and env vars are configured in the Render dashboard).
- **Port**: the app reads `$PORT` from the environment (Render injects it) and
  falls back to `7860` for local runs (see [src/chatbot/config.py](src/chatbot/config.py)).
- **Runtime**: Flask's built-in dev server (`app.run(...)`) — fine for this
  project's traffic, not a hardened production WSGI setup.

## Logging & observability

Render's **Logs** tab only captures **stdout/stderr** — it never reads files
the process writes to its own disk, and the container's disk isn't
persistent across restarts/redeploys anyway. So the app logs on two channels
that look similar but serve different purposes:

| Channel | Where | Survives a redeploy? | Visible in Render dashboard? |
|---|---|---|---|
| Structured JSON logs (`logging` → stdout) | `logging_config.py` | n/a (streamed live) | **Yes** — this is what you watch |
| `stage_log.jsonl` / `chat_log.csv` | local container disk | No | No |

The stdout logs are the source of truth in production. The two local files
still get written (same fields, same content) purely as a local-dev
convenience for tailing/grepping on your own machine; don't rely on them for
anything running on Render.

### What gets logged

One JSON object per line, via Python's standard `logging` module (no
`print()` calls left on the request path):

- **Startup**: index/database load results — `rag_index_loaded`,
  `text_to_sql_ready`, or a `WARNING`/`ERROR` line if something's missing.
- **Per request**: `chat_request_received` → one `stage_completed` line per
  stage (`route`, `retrieve`/`generate`, or `sql`) → `chat_request_completed`
  (or `chat_request_failed` with the traceback and which stage broke).
- **Rate limiting**: `rate_limit_exceeded` when Flask-Limiter's 20/min cap
  trips for an IP.

Severity is meaningful, not decorative:

- `ERROR` — a stage failed outright (`chat_request_failed`, `stage_completed`
  with `status: failed`, index/schema load failures).
- `WARNING` — worked, but only after a retry (`status: retried_ok`), or a
  config issue that degrades a feature (missing DB file, missing index).
- `INFO` — normal request/stage lifecycle.
- `DEBUG` — noisy per-candidate detail (e.g. every RAG chunk's similarity
  score); off by default.

Example lines you'll see in the Render Logs tab for one request:

```json
{"ts": "...", "level": "INFO", "logger": "chatbot.app", "message": "chat_request_received", "request_id": "e37724807b14", "query": "hey"}
{"ts": "...", "level": "INFO", "logger": "chatbot.reliability.run_record", "message": "stage_completed", "request_id": "e37724807b14", "stage": "route", "status": "ok", "attempts": 1, "duration_s": 1.8}
{"ts": "...", "level": "INFO", "logger": "chatbot.reliability.run_record", "message": "stage_completed", "request_id": "e37724807b14", "stage": "retrieve", "status": "empty", "attempts": 1, "duration_s": 0.57, "chunk_ids": []}
{"ts": "...", "level": "INFO", "logger": "chatbot.app", "message": "chat_request_completed", "request_id": "e37724807b14", "route": "rag", "status": "ok", "total_duration_s": 2.3}
```

`request_id` is the correlation key — search/filter Render's log viewer by it
to see one request's full story end to end.

### Controlling verbosity

Set the `LOG_LEVEL` env var on the Render service (default `INFO`):

- `LOG_LEVEL=DEBUG` — temporarily, while investigating a retrieval-quality
  issue, to also see every candidate chunk's similarity score.
- `LOG_LEVEL=WARNING` — quiet mode, only retries/failures/config issues.

### Why stdout is unbuffered

The [Dockerfile](Dockerfile) sets `ENV PYTHONUNBUFFERED=1`. Without it,
Python block-buffers stdout inside a container (it isn't a TTY), so log
lines can show up late, batched, or not at all before a crash — this is the
most common reason a container "isn't logging" when the code is fine.

## Local development

```bash
python -m venv venv
venv\Scripts\activate          # Windows; `source venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"        # installs the `chatbot` package (editable) + pytest
cp .env.example .env           # then fill in LLM_API_KEY
python -m chatbot.app
```

Requires a `.env` with `LLM_API_KEY` set — see [.env.example](.env.example) for
the full list of variables `chatbot.config` reads. Logs print as JSON lines to
your terminal the same way they do on Render.

### Where the models run

Two model workloads, two very different places:

| Workload | Where it runs | Configured by | Needs network? |
|---|---|---|---|
| Generation | remote, OpenAI-**compatible** endpoint | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | yes |
| Embeddings | **in-process**, sentence-transformers | `EMBED_MODEL`, `EMBED_DIM` | no |

Generation is provider-neutral on purpose: Gemini's free tier today, a local
Ollama server (`LLM_BASE_URL=http://localhost:11434/v1`) as the offline
fallback. Swapping between them is a `.env` edit, not a code change.

Embeddings never leave the machine, so retrieval cannot rate-limit, time out,
or fail on an expired token. The **same** model and the **same** D3
normalization (`rag/arabic.py`) run at index-build time and at query time —
if those two paths ever diverge, retrieval degrades with no error and no
failing test.

The embedding weights download from the HF hub on first use and are then
cached (`HF_HOME`). That download is free and needs no token — these are
public models.

### Running tests

```bash
pytest
```

Covers the SQL safety guard + happy/guard-path answering
(`tests/test_sql_engine.py`), the retry/backoff runner
(`tests/test_stage_runner.py`), and the retry-vs-terminal error classifier
(`tests/test_failure_classifier.py`) — no network calls, no waiting on real
backoff delays.

## Building the RAG index (offline)

`scripts/build_index.py` reads every PDF in `data/source_docs/`, extracts
text (with an OCR fallback for scanned pages/PDFs), chunks and embeds it, and
writes `data/index/embeddings.npy` + `data/index/chunks.json`. Only those two
artifacts (plus `src/chatbot/rag/retrieval.py`) are needed at deploy time —
the build itself runs locally:

```bash
pip install -e ".[build]"   # adds pytesseract + pdf2image on top of the base deps
python scripts/build_index.py
```

Tesseract/poppler are build-machine-only system packages, never installed in
the Dockerfile:

- Debian/Ubuntu: `sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara poppler-utils`
- macOS: `brew install tesseract tesseract-lang poppler`

Each page is tried with `pypdf` first; if it has no usable text layer, it
falls back to Tesseract OCR (`eng+ara`). The build prints a per-page
`text_layer`/`ocr` summary, and each chunk in `chunks.json` carries an
`extraction_source` field so weak-OCR pages can be traced later.