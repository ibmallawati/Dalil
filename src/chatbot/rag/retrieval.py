"""
Retrieval module — prebuilt index, pluggable embedding backend.

Two things happen here. The SEARCH is always local: precomputed vectors are
loaded once at startup and compared with one matrix multiply, so the corpus
never leaves the machine. Only the act of turning text INTO a vector is
pluggable.

    embed_query(...)     -> use for questions
    embed_passages(...)  -> use for corpus text (build_index.py)

Every caller uses those two. Neither knows or cares which backend is behind
them, which is what made swapping backends a contained change.

--- Why the default is no longer in-process -------------------------------

It used to be. Running e5-base locally meant retrieval could not rate-limit,
could not time out, and could not fail on an expired token — a genuinely
valuable property, and the reason it was chosen.

It was given up for one measured reason. On this machine:

    app with the index loaded, no model .......    87 MB
    after e5-base loads .......................   792 MB
    peak while embedding one question .........  1,143 MB

The model is 705 MB of that. Free hosts offer 512 MB; none offer 1.2 GB. So
the choice was between in-process embeddings and being deployed at all.

What that costs is stated plainly, because it is a real regression: the
retrieval path now depends on the network. It can rate-limit and it can
time out. Generation already could, and has a failover chain for it;
retrieval does not yet.

--- The backends ----------------------------------------------------------

gemini  (default)  Embeds over the same OpenAI-compatible endpoint used for
                   generation, so no second credential. Vectors are
                   requested at settings.embed_dim and RE-NORMALISED, which
                   is not optional — see _embed_gemini.

local              intfloat/multilingual-e5-base, in-process. Needs
                   `pip install sentence-transformers`, deliberately absent
                   from requirements.txt. Kept because every retrieval
                   threshold in this project was tuned against it, so it is
                   the baseline any new backend has to be compared with.

--- E5 prefixes: local-only, and silent when wrong ------------------------

intfloat/multilingual-e5-* is asymmetric. It was trained with literal
prefixes and expects them:

    documents -> "passage: <text>"
    questions -> "query: <text>"

Omitting them raises nothing; it quietly costs retrieval quality. They are
applied inside the two functions above rather than left to callers to
remember. They apply ONLY to the local backend — Gemini was not trained
with them, and adding them there would embed the literal English word
"query" into every Arabic question.
"""

import json
import logging
import re

import numpy as np

from chatbot.config import settings
from chatbot.rag.arabic import normalize_arabic

logger = logging.getLogger(__name__)

# E5's required asymmetric prefixes. Not stylistic — part of the model.
# How many texts go in one embeddings request. Only the index build ever
# exceeds this; a question is always a batch of one.
EMBED_BATCH = 100

QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

_model = None


def get_model():
    """
    Load the sentence-transformers model once, on first use.

    Only reachable with EMBED_BACKEND=local. Lazy rather than at import:
    the weights are hundreds of MB, and importing this module must stay
    cheap for tests, scripts and tooling that only want retrieve()'s
    helpers or the loaded chunk metadata.
    """
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:      # pragma: no cover - install-time path
            raise RuntimeError(
                "EMBED_BACKEND=local needs sentence-transformers, which is no "
                "longer in requirements.txt because it pulls in ~750 MB of "
                "torch. Install it explicitly to use the local backend:\n"
                "    pip install sentence-transformers"
            ) from exc

        logger.info("embedding_model_loading", extra={"model": settings.embed_model})
        _model = SentenceTransformer(settings.embed_model)
        dim = _model.get_sentence_embedding_dimension()
        logger.info(
            "embedding_model_loaded",
            extra={"model": settings.embed_model, "dim": dim},
        )
        # Catch a model/config mismatch at load, not at the first request.
        if dim != settings.embed_dim:
            logger.error(
                "embed_dim_mismatch",
                extra={
                    "configured_embed_dim": settings.embed_dim,
                    "actual_model_dim": dim,
                    "hint": "set EMBED_DIM to the model's real width and rebuild the index",
                },
            )
    return _model


def _l2_normalise(vectors):
    """
    Scale every row to unit length.

    retrieve() treats a dot product AS cosine similarity, which is only true
    for unit vectors. Every threshold in this project — the D2 similarity
    floor, the ambiguity margin — is a number on that scale, so a vector
    that is not unit length rescales all of them at once, silently.
    """
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    # A zero vector cannot be normalised; leave it rather than divide by 0.
    np.divide(vectors, norms, out=vectors, where=norms > 0)
    return vectors


_RETRY_AFTER = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


def _embed_request(prepared):
    """
    One embeddings call, retried when the free tier's per-minute cap trips.

    The cap is 100 embed requests per minute, and the index build spends
    that in a single batch — so a 429 during a rebuild is expected, not
    exceptional, and the build must wait rather than die halfway with a
    half-written index.

    A QUERY is different: a citizen is waiting. Interactive calls get one
    short retry and then fail honestly, because a 40-second pause is worse
    than a refusal.
    """
    import time

    from openai import RateLimitError

    from chatbot.llm import client

    interactive = len(prepared) == 1
    attempts = 2 if interactive else 6
    max_wait = 5.0 if interactive else 75.0

    for attempt in range(1, attempts + 1):
        try:
            return client.embeddings.create(
                model=settings.embed_model,
                input=prepared,
                dimensions=settings.embed_dim,
            )
        except RateLimitError as exc:
            if attempt == attempts:
                raise
            # Google states the wait in the error text; honour it rather
            # than guessing, but never block a request path for that long.
            match = _RETRY_AFTER.search(str(exc))
            wait = float(match.group(1)) + 1 if match else 20.0 * attempt
            wait = min(wait, max_wait)
            logger.warning("embed_rate_limited", extra={
                "attempt": attempt, "of": attempts, "waiting_s": round(wait, 1),
                "texts": len(prepared), "interactive": interactive})
            time.sleep(wait)


def _embed_gemini(texts):
    """
    Embed via the OpenAI-compatible endpoint already used for generation.

    Requests settings.embed_dim directly. gemini-embedding-001 is natively
    3072-wide and truncates on request (Matryoshka), which keeps the index
    at ~1.2 MB instead of ~4.7 MB.

    THE RE-NORMALISATION IS LOAD-BEARING. A truncated vector is no longer
    unit length — measured on this model, the norm is 1.0000 at 3072 dims
    and 0.5790 at 768. Skipping it does not raise; it multiplies every
    similarity score by roughly 0.58 and quietly invalidates every threshold
    tuned against the old scale.
    """
    prepared = [normalize_arabic(t) for t in texts]

    # Split because build_index.py hands over the whole corpus in one call —
    # 379 chunks today, more as it grows.
    #
    # Batching does NOT save quota: the free tier counts 100 embed requests
    # per minute and a batch of 100 texts spends all 100. It only saves HTTP
    # round trips, and it bounds what one 429 costs.
    if len(prepared) > EMBED_BATCH:
        return _l2_normalise(np.vstack([
            _embed_gemini(prepared[i:i + EMBED_BATCH])
            for i in range(0, len(prepared), EMBED_BATCH)
        ]))

    response = _embed_request(prepared)
    # Order matters: row N must be the vector for text N, or the whole index
    # is silently shuffled. Sort by `index` when the API populates it —
    # Gemini's OpenAI-compatibility layer returns None there and relies on
    # response order, so fall back to that rather than crashing on the sort.
    data = list(response.data)
    if all(getattr(item, "index", None) is not None for item in data):
        data.sort(key=lambda item: item.index)
    if len(data) != len(prepared):
        raise RuntimeError(
            f"asked for {len(prepared)} embeddings, got {len(data)}. Refusing "
            f"to build a misaligned index.")
    vectors = np.asarray([item.embedding for item in data], dtype=np.float32)

    if vectors.shape[1] != settings.embed_dim:
        raise RuntimeError(
            f"{settings.embed_model} returned {vectors.shape[1]}-dim vectors, "
            f"expected {settings.embed_dim}. The index and the query must be "
            f"the same width — set EMBED_DIM and rebuild the index.")
    return _l2_normalise(vectors)


def _embed_local(texts, prefix):
    """Normalize (D3) -> prefix (E5) -> encode -> L2-normalize."""
    prepared = [prefix + normalize_arabic(t) for t in texts]
    vectors = get_model().encode(
        prepared,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return np.asarray(vectors, dtype=np.float32)


def _encode(texts, prefix):
    """Turn text into unit vectors, using whichever backend is configured."""
    if not texts:
        return np.zeros((0, settings.embed_dim), dtype=np.float32)
    if settings.embed_backend == "local":
        return _embed_local(texts, prefix)
    return _embed_gemini(texts)


def embed_query(question: str) -> np.ndarray:
    """Embed ONE user question. Returns a 1-D unit vector."""
    return _encode([question], QUERY_PREFIX)[0]


def embed_passages(texts) -> np.ndarray:
    """
    Embed a list of corpus chunks. Returns a 2-D array, one row per text.

    Batched deliberately: build_index.py re-embeds the whole corpus every
    time the team re-curates a service file, and per-item encoding calls
    would make that slow enough that people avoid rebuilding.
    """
    return _encode(list(texts), PASSAGE_PREFIX)


# --- Load the prebuilt index at import time (cheap: numpy + json only) -----
if settings.index_file.exists() and settings.chunks_file.exists():
    _doc_embeddings = np.load(settings.index_file)
    with open(settings.chunks_file, encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)

    # Tripwire. An index built by a different embedding model has a
    # different width. Without this, the mismatch surfaces either as an
    # opaque NumPy shape error at request time or — if the widths happen to
    # agree — as silently meaningless similarity scores, which would poison
    # D2's threshold and D7's accuracy figure with nothing in the logs.
    if _doc_embeddings.ndim != 2 or _doc_embeddings.shape[1] != settings.embed_dim:
        logger.error(
            "rag_index_dim_mismatch",
            extra={
                "index_shape": list(_doc_embeddings.shape),
                "expected_dim": settings.embed_dim,
                "hint": "stale index - re-run scripts/build_index.py",
            },
        )
        _doc_embeddings = None
        KNOWLEDGE = []
    else:
        logger.info(
            "rag_index_loaded",
            extra={
                "chunk_count": len(KNOWLEDGE),
                "dim": int(_doc_embeddings.shape[1]),
                "index_file": str(settings.index_file),
            },
        )
else:
    _doc_embeddings = None
    KNOWLEDGE = []
    logger.warning(
        "rag_index_missing",
        extra={
            "index_file": str(settings.index_file),
            "chunks_file": str(settings.chunks_file),
        },
    )


def fetch_service(service_id):
    """
    Return EVERY section of one service, in the order the curator wrote them.

    No embedding, no similarity, no k. Once L1 has resolved which service the
    citizen means, similarity search is the wrong tool:

      - D5's answer card has eight fields. Top-k over a seven-section service
        structurally cannot fill them all. The model then either nulls the
        missing fields or supplies them from general knowledge — producing an
        invented fee underneath a real government citation. That is the worst
        output this system can generate.
      - A curated service file is 20-40 lines. There is nothing to select
        from; selection only loses information.
      - It sidesteps the threshold entirely. The measured in-corpus /
        out-of-corpus separation on e5 is ~0.015, so for the common case —
        a question that names its service — no similarity score is consulted
        at all and that narrow gap simply does not apply.

    Document order matters: sections come back as written (eligibility,
    documents, steps, fees, ...), which is the order a citizen reads them.
    """
    if not KNOWLEDGE:
        return []
    return [
        {**chunk, "chunk_id": i, "score": None, "retrieval": "whole_service"}
        for i, chunk in enumerate(KNOWLEDGE)
        if chunk.get("service_id") == service_id
    ]


def retrieve(question, k=3):
    """
    Return the top-k chunks whose similarity clears the D2 threshold.

    Each result is the stored chunk dict plus `score` and `chunk_id`.
    Returns [] when nothing clears the bar — that empty list is what drives
    the D6 refusal paths, so it is a meaningful result, not a failure.
    """
    if _doc_embeddings is None:
        return []

    q_emb = embed_query(question)
    similarities = _doc_embeddings @ q_emb          # cosine (vectors are unit)
    top_ids = np.argsort(similarities)[::-1][:k]

    threshold = settings.similarity_threshold
    results = []
    for i in top_ids:
        score = float(similarities[i])
        # DEBUG, not INFO: this fires per-candidate on every request. Set
        # LOG_LEVEL=DEBUG to watch scores while tuning the D2 threshold.
        logger.debug(
            "rag_candidate_scored",
            extra={
                "score": round(score, 4),
                "threshold": threshold,
                "chunk_id": int(i),
                "source": KNOWLEDGE[i].get("source"),
            },
        )
        if score >= threshold:
            results.append({
                **KNOWLEDGE[i],
                "score": score,
                "chunk_id": int(i),
            })
    return results
