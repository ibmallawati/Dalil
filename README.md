---
title: Daleel
emoji: 🧭
colorFrom: green
colorTo: red
sdk: docker
app_port: 7860
pinned: false
short_description: مساعد عربي لإجراءات الخدمات الحكومية في سلطنة عُمان
---

# دليل / Daleel

An Arabic assistant for Omani government service procedures. Ask how to renew
a passport, what a civil-ID replacement costs, or which documents a commercial
registration needs — and get an answer taken from a curated corpus, with the
source attached.

Built for the MTCIT **Engineer it with AI** national competition
(Programming & Development track).

**The one rule the whole design serves:** never state a government fee,
document or deadline that is not in the corpus. A wrong fee costs a citizen a
wasted trip to a counter, so the system refuses far more readily than it
guesses.

## How a question is answered

```
question
   │
   ├─ L1  registry     does Daleel cover this authority at all?
   │                   Arabic keyword match, no model, no embedding.
   │                   An uncovered ministry is refused here, for free.
   │
   ├─ L2  retrieval    embed the question, cosine-match the corpus.
   │                   Runs for every question that gets past L1.
   │                   Two services scoring near-identically -> ask which.
   │
   └─ L3  grounding    the model answers AND reports whether the passages
                       actually supported it. "none" becomes a refusal
                       rather than a confident-looking guess.
```

L1 is a **hint**, not a gate: a resolved service guarantees its sections reach
the model, but never suppresses retrieval. That matters for questions spanning
two services — «كم رسوم تجديد جواز السفر ورخصة السياقة؟» is answered from both
files, and cited to both.

Citations are never the model's to choose. Provenance is attached afterwards
from corpus metadata, and narrowed to the passages the answer can actually be
traced to.

## Deployment

Runs as a **Docker Space** on Hugging Face. The `sdk: docker` and
`app_port: 7860` above are what configure that — the Dockerfile listens on
`$PORT` and falls back to 7860.

### Required secret

| Name | Where | Notes |
|---|---|---|
| `LLM_API_KEY` | Space → Settings → **Variables and secrets** → *New secret* | Must be a **secret**, not a variable. Variables are publicly visible. |

Optional, all with working defaults in
[`src/chatbot/config.py`](src/chatbot/config.py): `LLM_BASE_URL`, `LLM_MODEL`,
`EMBED_MODEL`, `LOG_LEVEL`.

`LLM_MODEL` accepts a comma-separated failover chain, tried in order:

```
LLM_MODEL=gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite
```

A per-day quota exhaustion is terminal and moves to the next model; a
per-minute limit is retried. The model that actually answered is reported in
the response and shown in the UI.

### First boot is slow

The embedding model (~1.1 GB) downloads on first start and is then cached, and
the app warms it before serving so the first citizen question is not the one
that pays for it. Expect a few minutes on a cold Space build.

## Running locally

```bash
python -m venv venv
venv\Scripts\activate         # Windows;  source venv/bin/activate elsewhere
pip install -e ".[dev]"
cp .env.example .env          # then fill in LLM_API_KEY
python -m chatbot.app
```

```bash
pytest                        # no network, no real backoff waits
```

## Where the models run

| Workload | Where | Configured by | Needs network? |
|---|---|---|---|
| Generation | remote, OpenAI-**compatible** endpoint | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | yes |
| Embeddings | **in-process**, sentence-transformers | `EMBED_MODEL`, `EMBED_DIM` | no |

Generation is provider-neutral on purpose: Gemini's free tier today, a local
Ollama server (`LLM_BASE_URL=http://localhost:11434/v1`) as the offline
fallback. Swapping is a `.env` edit, not a code change.

Embeddings never leave the process, so **retrieval cannot rate-limit, time out
or fail on an expired token** — only generation can. The same model and the
same Arabic normalisation (`rag/arabic.py`) run at index-build time and at
query time; if those two paths ever diverge, retrieval degrades silently with
no failing test, which is why they share one module.

## The corpus

One markdown file per service in `data/source_docs/`, each carrying
front-matter that every chunk inherits — authority, service, and a citation.

```yaml
---
authority: شرطة عمان السلطانية
authority_en: Royal Oman Police
service: تجديد جواز السفر
service_id: rop-passport-renewal     # must equal the filename stem
source_url: https://www.rop.gov.om/...   # a URL, or…
source_ref: دليل الخدمات الحكومية، ص ١٤   # …a text citation, for print sources
retrieved: 2026-08-17
verified_by: PENDING
---
```

`service_id` is the join key to `data/registry/authorities.json`. One character
of drift and the service refuses every question while sitting fully indexed, so
the build enforces the match. See [docs/CORPUS_SPEC.md](docs/CORPUS_SPEC.md).

### Adding content

**Hand-written markdown** — drop a file in `data/source_docs/`, then:

```bash
python scripts/sync_registry.py --write   # declare it so questions can reach it
python scripts/build_index.py             # re-embed
```

**From a PDF** — drop it in `data/pdf_inbox/`, then:

```bash
python scripts/ocr_inbox.py               # PDF  -> data/pdf_ocr/*.ocr.txt
python scripts/import_ocr_corpus.py       # dry run: what would be imported
python scripts/import_ocr_corpus.py --write
python scripts/build_index.py
```

Every page is OCR'd, including pages that already contain digital text: the
ministry PDFs carried Arabic with `U+064B` in place of the space character,
which is long enough to pass any "does this page have text?" check and fuses
every word once tashkeel is stripped. One extraction path cannot silently pick
the wrong one.

The importer uses **no LLM**. The version it replaced asked a model to
restructure OCR text and it repaired damage instead of reporting it —
`«.تختلة 1 نوع النشاط والتر خب»` became a fluent, invented
`«تختلف حسب نوع النشاط والترخيص»`. Everything now is a pure string operation,
so there is nothing to fabricate and nothing to validate. OCR typos reach the
corpus unfixed; a visible typo is a far better failure than an invented fee.

See [docs/PDF_INTAKE_SPEC.md](docs/PDF_INTAKE_SPEC.md).

## Project structure

```
src/chatbot/
  app.py                    # Flask routes; entry point: python -m chatbot.app
  config.py                 # every env var read and validated in one place
  llm.py                    # provider-neutral generation + failover chain
  agent/
    registry.py             # L1: Arabic keyword classification, no model
    router.py               # picks how to gather grounding, and why
    answer.py               # L2/L3: one call produces answer + confidence
    card.py                 # provenance and the confidence verdict
  rag/
    retrieval.py            # embeds and retrieves (local sentence-transformers)
    markdown_corpus.py      # loads + validates the curated corpus
    arabic.py               # Arabic normalisation, shared by index and query
    registry_sync.py        # keeps every corpus file routable
    ocr.py                  # build-time only; never ships to the request path
  reliability/              # per-stage retry/backoff, and honest failure reporting
  sql/                      # legacy text-to-SQL from the fork; not in the request path
scripts/
  build_index.py            # corpus  -> embeddings.npy + chunks.json
  ocr_inbox.py              # PDFs    -> OCR text
  import_ocr_corpus.py      # OCR text-> corpus files, mechanically
  sync_registry.py          # corpus  -> registry entries
data/
  source_docs/              # the corpus, one markdown file per service
  registry/authorities.json # what Daleel claims to cover
  index/                    # prebuilt embeddings + chunks (committed)
  pdf_inbox/                # drop PDFs here (the PDFs themselves are gitignored)
tests/                      # pytest; no network
```

The `sql/` package is kept from the fork this project started as. It is not on
the request path — Daleel has no database — but its tests still run and it is
part of the project's history.

## Logging

One JSON object per line to **stdout**, which is what the Space's log viewer
reads. Files under `logs/` are written too, but only as a local convenience —
a container's disk does not survive a restart.

- **Startup** — `rag_index_loaded`, `embedder_warmup_complete`, or a
  `WARNING`/`ERROR` if something is missing.
- **Per request** — `chat_request_received` → one `stage_completed` per stage
  → `chat_request_completed`, or `chat_request_failed` with the stage that
  broke.
- **Correlation** — every line carries `request_id`; filter by it to read one
  request end to end.

Severity is meaningful, not decorative: `ERROR` is a stage that failed,
`WARNING` is something that worked only after a retry or a config problem that
degrades a feature, `INFO` is the normal lifecycle. `LOG_LEVEL=DEBUG` adds
per-chunk similarity scores when investigating retrieval quality.

The Dockerfile sets `PYTHONUNBUFFERED=1`. Without it Python block-buffers
stdout inside a container, and log lines arrive late, batched, or not at all
before a crash — the most common reason a container "isn't logging" when the
code is fine.
