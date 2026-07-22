"""
Retrieval module - loads a PREBUILT embedding index (no local model, no torch).

Embeddings come from the Hugging Face Inference API, so this module has no
sentence-transformers / torch dependency. That keeps the deployed container
small enough for free, no-card hosts.

Query flow (runtime):
- retrieve() embeds the user's question via the API, then does NumPy cosine
  similarity against the prebuilt chunk vectors loaded from embeddings.npy.

Build flow (offline, run once whenever the PDFs change):
- python scripts/build_index.py   ->   regenerates data/index/embeddings.npy + chunks.json

Setup: pip install pypdf huggingface_hub numpy
Requires the HF_TOKEN env var (same token as the chat model; it must have the
"Make calls to Inference Providers" permission).
"""

import logging
import json
import numpy as np
from huggingface_hub import InferenceClient

from chatbot.config import settings

logger = logging.getLogger(__name__)

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SIMILARITY_THRESHOLD = 0.30       # minimum score a chunk must reach to be used

# One shared client. Uses the same HF_TOKEN as your chat model.
_client = InferenceClient(provider="hf-inference", api_key=settings.hf_token)


def embed(text):
    """
    Return a single L2-normalized 384-dim vector for `text` from the HF API.

    Two safety steps make this robust:
    - if the API returns per-token vectors (2D), mean-pool them into one vector,
    - normalize the result so a dot product equals cosine similarity (this keeps
      SIMILARITY_THRESHOLD comparable to your old sentence-transformers scores).
    """
    vec = np.asarray(
        _client.feature_extraction(text, model=EMBED_MODEL), dtype=np.float32
    )
    if vec.ndim == 2:                     # per-token output -> mean pool
        vec = vec.mean(axis=0)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


# --- Load the prebuilt index at import time (no PDFs read, no model loaded) ---
if settings.index_file.exists() and settings.chunks_file.exists():
    _doc_embeddings = np.load(settings.index_file)
    with open(settings.chunks_file, encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)
    logger.info(
        "rag_index_loaded",
        extra={"chunk_count": len(KNOWLEDGE), "index_file": str(settings.index_file)},
    )
else:
    _doc_embeddings = None
    KNOWLEDGE = []
    logger.warning(
        "rag_index_missing",
        extra={"index_file": str(settings.index_file), "chunks_file": str(settings.chunks_file)},
    )


def retrieve(question, k=3):
    """
    Return the top-k chunks whose similarity score clears SIMILARITY_THRESHOLD.
    Each result is a dict with keys: text, source, page, score, chunk_id.
    Returns an empty list if nothing clears the bar (or if no index is loaded).
    """
    if _doc_embeddings is None:
        return []

    q_emb = embed(question)
    similarities = _doc_embeddings @ q_emb          # cosine (vectors are normalized)
    top_ids = np.argsort(similarities)[::-1][:k]    # top-k indices

    results = []
    for i in top_ids:
        score = float(similarities[i])
        # DEBUG, not INFO: this fires per-candidate on every RAG request,
        # so it'd otherwise drown out real events. Set LOG_LEVEL=DEBUG to see it.
        logger.debug(
            "rag_candidate_scored",
            extra={
                "score": round(score, 4),
                "chunk_id": int(i),
                "source": KNOWLEDGE[i]["source"],
                "page": KNOWLEDGE[i]["page"],
            },
        )
        if score >= SIMILARITY_THRESHOLD:
            results.append({
                **KNOWLEDGE[i],
                "score": score,
                "chunk_id": int(i),   # included so app.py can log it
            })
    return results
