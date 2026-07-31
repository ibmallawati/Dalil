"""
Retrieval module — LOCAL embeddings, prebuilt index, no network.

--- What changed, and why -------------------------------------------------

This module used to call a remote embedding API so the deployed container
could stay small enough for a free host. Hosting is no longer a constraint,
and the project runs under a hard $0 budget, so the model now runs
IN-PROCESS via sentence-transformers at both index-build time and query
time.

The consequence is the point: the retrieval path never touches the network.
It cannot rate-limit, cannot time out, cannot fail because a token expired
the morning of a demo. The only remote call left in the whole request is
generation.

The prebuilt index is still prebuilt (scripts/build_index.py -> npy+json).
Loading precomputed vectors keeps startup fast; only the QUERY is embedded
at request time.

--- E5 prefixes: the thing that breaks silently ---------------------------

intfloat/multilingual-e5-* is an asymmetric model. It was trained with
literal prefixes and expects them:

    documents -> "passage: <text>"
    questions -> "query: <text>"

Omitting them raises nothing. It just quietly costs retrieval quality, and
it would do so AFTER D2 has set a threshold and D7 has produced an accuracy
number — corrupting both without a single error in the logs. That is why
the prefixes are applied here, inside the only two functions anyone is
supposed to call, rather than left to each caller to remember.

    embed_query(...)     -> use for questions
    embed_passages(...)  -> use for corpus text (build_index.py)
"""

import json
import logging

import numpy as np

from chatbot.config import settings
from chatbot.rag.arabic import normalize_arabic

logger = logging.getLogger(__name__)

# E5's required asymmetric prefixes. Not stylistic — part of the model.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

_model = None


def get_model():
    """
    Load the sentence-transformers model once, on first use.

    Lazy rather than at import: the weights are hundreds of MB, and
    importing this module must stay cheap for tests, scripts and tooling
    that only want retrieve()'s helpers or the loaded chunk metadata.
    """
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

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


def _encode(texts, prefix):
    """
    Normalize (D3) -> prefix (E5) -> encode -> L2-normalize.

    normalize_embeddings=True means a dot product IS cosine similarity, so
    retrieve() can use a single matrix multiply instead of dividing by norms
    on every request.
    """
    prepared = [prefix + normalize_arabic(t) for t in texts]
    vectors = get_model().encode(
        prepared,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return np.asarray(vectors, dtype=np.float32)


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
