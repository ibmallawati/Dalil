"""
config.py — single source of truth for environment-driven settings.

Every other module reads its configuration from `settings` here instead of
calling os.environ.get() directly, so the full set of things this app
depends on (keys, model ids, file locations) is visible in one place
instead of scattered across app.py / rag/retrieval.py / scripts.

Importing this module loads .env and validates LLM_API_KEY immediately
(fails fast on missing config).

--- Architecture (post-D1 revision) --------------------------------------

Daleel splits its two model workloads across two very different places:

  GENERATION  -> a remote, OpenAI-COMPATIBLE chat endpoint.
                 Configured entirely by env vars (LLM_BASE_URL, LLM_MODEL,
                 LLM_API_KEY) so the provider is swappable WITHOUT a code
                 change. Today: Gemini's free tier. Demo-day fallback:
                 a local Ollama server. Same client code for both.

  EMBEDDINGS  -> fully LOCAL, in-process, via sentence-transformers.
                 No API, no key, no network. Identical model at index-build
                 time and query time.

Cost constraint: this project must run at $0. Nothing here may point at a
paid service by default.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# src/chatbot/config.py -> src/chatbot -> src -> repo root
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

# --- Generation defaults --------------------------------------------------
# Gemini exposes an OpenAI-compatible surface, so the existing `openai`
# client shape survives the provider swap untouched. Pointing this at a
# local Ollama server (http://localhost:11434/v1) is a .env edit, not a
# code edit — which is exactly the offline demo-day fallback.
DEFAULT_LLM_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
# Ordered failover chain. Every entry was verified to answer correctly in
# Arabic on 31 Jul 2026 against a real free-tier key.
#
# gemini-3.5-flash is deliberately ABSENT: its free-tier ceiling is 20
# requests per DAY, which one rehearsal exhausts.
DEFAULT_LLM_MODEL = "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"

# Gemini 3.x Flash is a THINKING model, and its reasoning tokens are billed
# against max_tokens. Measured on gemini-3.6-flash, "what is the capital of
# Oman" burned 305 reasoning tokens to produce a 12-token answer.
#
# Two things follow, both of which would have surfaced mid-demo:
#   - a max_tokens budget sized for the ANSWER silently truncates it. At
#     max_tokens=100 the model returned the string 'Information Retrieval'
#     — a leaked internal-thought fragment served as the reply.
#   - D5's JSON card is the long output on this project. Truncated JSON
#     fails to parse, so every answer would fall back to plain prose.
#
# Measured: reasoning_effort="minimal" -> 0 reasoning tokens, same answer.
# "low" still burned 265. "none" is rejected with a 400.
#
# Set to an empty string for providers that reject the parameter (a local
# Ollama server will 400 on it) — it is only sent when non-empty.
DEFAULT_LLM_REASONING_EFFORT = "minimal"

# Sized from the WORST case, not a friendly one. Measured 31 Jul 2026:
#
#   short fixture service (6 short sections)     198 completion tokens
#   realistic long service (15 documents,
#     12 steps, tiered fees, notes)              832 completion tokens
#
#   at max_tokens=500  -> finish_reason='length', JSON DOES NOT PARSE
#   at max_tokens=1000 -> complete, all 15 documents and 12 steps present
#
# Card length is driven by LIST fields (required_documents, steps), not by
# prose length: a long but repetitive service gets summarised, while one
# with many distinct documents does not. So the worst case is a service
# with a long checklist, which is exactly what a government service is.
#
# 2000 is ~2.4x the measured worst case. The headroom is free — the free
# tier is limited by requests per day, not tokens, and unused budget is
# never spent. Truncation, by contrast, costs the entire answer: unparsed
# JSON drops every response onto the plain-prose fallback.
DEFAULT_LLM_MAX_TOKENS = 2000

# --- Embedding defaults ---------------------------------------------------
# intfloat/multilingual-e5-base: 768-dim, strong Arabic, runs on CPU.
#
# E5 IS PREFIX-SENSITIVE. Documents must be embedded as "passage: <text>"
# and questions as "query: <text>". Omitting the prefixes does not raise —
# it silently degrades retrieval, which is the worst possible failure here
# because it would corrupt D2's threshold and D7's accuracy number without
# any visible error. The prefixes live in chatbot.rag.retrieval; they are
# not optional.
#
# Lighter fallback if this is too heavy locally (also update EMBED_DIM=384):
#   sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-base"
DEFAULT_EMBED_DIM = 768

# --- D2: retrieval similarity threshold ----------------------------------
# PROVISIONAL. Not yet evidence-based; do not quote it as if it were.
#
# Similarity scores are NOT portable across embedding models. The original
# 0.30 was derived for all-MiniLM-L6-v2 and is meaningless here.
#
# E5 in particular compresses its score range: cosine similarity between
# two UNRELATED Arabic texts commonly still lands around 0.70-0.80, so the
# usable threshold sits high and the in-corpus/out-of-corpus gap is narrow.
# Guessing this number is precisely how Daleel ends up confidently
# answering a question it has no grounds for, live, in front of judges.
#
# D2 replaces this by running the D7 evaluation set, printing the top score
# per question, and choosing from the gap between the two distributions.
# Read from the environment so that sweep needs no code edit.
DEFAULT_SIMILARITY_THRESHOLD = 0.80

# --- Margin-based ambiguity (L2) -----------------------------------------
# PROVISIONAL, same caveat as the threshold above.
#
# On the similarity path, if the best chunk of one service and the best
# chunk of a DIFFERENT service score within this margin of each other, the
# question is treated as ambiguous and Daleel asks rather than silently
# taking top-1.
#
# This catches ambiguity the registry has not been told about yet. The
# registry handles declared ambiguity (see ambiguous_phrases); this handles
# the undeclared kind, which is the kind nobody has thought of.
#
# Given e5's compressed score range, a "small" margin here is genuinely
# small. D2 derives the real value alongside the threshold.
DEFAULT_AMBIGUITY_MARGIN = 0.02


@dataclass(frozen=True)
class Settings:
    # generation (remote, OpenAI-compatible, provider-neutral)
    llm_api_key: str
    llm_base_url: str
    llm_models: tuple          # ordered failover chain; [0] is preferred
    llm_reasoning_effort: str
    llm_max_tokens: int

    @property
    def llm_model(self) -> str:
        """The preferred model. Kept for display and logging."""
        return self.llm_models[0]
    # embeddings (local, in-process)
    embed_model: str
    embed_dim: int
    # retrieval
    similarity_threshold: float
    ambiguity_margin: float
    # runtime
    port: int
    log_level: str
    db_paths: list
    # paths
    index_file: Path
    chunks_file: Path
    source_docs_dir: Path
    eval_file: Path
    chat_log_file: Path
    stage_log_file: Path


def load_settings() -> Settings:
    # Fail fast: nothing in this package imports without a generation key.
    # (A local Ollama endpoint ignores the value, but still needs one set —
    # use any placeholder string in that case.)
    llm_api_key = os.environ.get("LLM_API_KEY")
    if not llm_api_key:
        # Named for BOTH places this runs. Deployed, there is no .env file to
        # copy, and being told to make one sends you looking for a filesystem
        # fix to what is actually a dashboard setting.
        raise RuntimeError(
            "LLM_API_KEY is missing.\n"
            "  Local:  copy '.env.example' to '.env' and fill it in.\n"
            "  Hugging Face Spaces:  Settings -> Variables and secrets -> "
            "New secret, named LLM_API_KEY.\n"
            "  (A Space SECRET, not a variable — variables are public.)")

    default_db = DATA_DIR / "databases" / "omantel_demo.db"
    db_paths_raw = os.environ.get("DB_PATHS", str(default_db))
    db_paths = [p.strip() for p in db_paths_raw.split(",") if p.strip()]

    index_dir = Path(os.environ.get("INDEX_DIR", DATA_DIR / "index"))

    # LLM_MODEL is an ORDERED FAILOVER CHAIN, comma-separated.
    #
    # Free-tier quotas are per-model and some are per-DAY: gemini-3.5-flash
    # allows 20 requests a day, and one rehearsal exhausts it. Recovering by
    # editing .env is not something anyone should be doing in front of a
    # panel, so exhaustion falls through to the next model automatically
    # (see chatbot.llm) and the answer records which model served it.
    llm_models = tuple(
        m.strip() for m in os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL).split(",")
        if m.strip()
    ) or (DEFAULT_LLM_MODEL,)

    LOGS_DIR.mkdir(exist_ok=True)

    return Settings(
        llm_api_key=llm_api_key,
        llm_base_url=os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        llm_models=llm_models,
        llm_reasoning_effort=os.environ.get(
            "LLM_REASONING_EFFORT", DEFAULT_LLM_REASONING_EFFORT
        ).strip(),
        llm_max_tokens=int(os.environ.get("LLM_MAX_TOKENS", DEFAULT_LLM_MAX_TOKENS)),
        embed_model=os.environ.get("EMBED_MODEL", DEFAULT_EMBED_MODEL),
        # Tripwire, not decoration: an embeddings.npy built by a different
        # model has a different width. Without this check that surfaces as
        # an opaque NumPy shape error at request time — or, if the widths
        # happen to match, as silently wrong similarity scores.
        embed_dim=int(os.environ.get("EMBED_DIM", DEFAULT_EMBED_DIM)),
        similarity_threshold=float(
            os.environ.get("SIMILARITY_THRESHOLD", DEFAULT_SIMILARITY_THRESHOLD)
        ),
        ambiguity_margin=float(
            os.environ.get("AMBIGUITY_MARGIN", DEFAULT_AMBIGUITY_MARGIN)
        ),
        port=int(os.environ.get("PORT", 7860)),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        db_paths=db_paths,
        # INDEX_DIR exists so development can run against data/fixtures/index
        # without touching the real artifacts. Fixtures are invented content;
        # build_index.py refuses to write them into the real index, and no
        # number measured against them may be quoted as a result.
        index_file=index_dir / "embeddings.npy",
        chunks_file=index_dir / "chunks.json",
        # Curated Arabic markdown, one file per government service (see
        # scripts/build_index.py). The PDF/OCR route remains as the
        # documented path to scale.
        source_docs_dir=DATA_DIR / "source_docs",
        # D7: the Arabic evaluation set, which doubles as the retrieval
        # regression suite.
        eval_file=DATA_DIR / "eval" / "eval_set.csv",
        chat_log_file=LOGS_DIR / "chat_log.csv",
        stage_log_file=LOGS_DIR / "stage_log.jsonl",
    )


settings = load_settings()
