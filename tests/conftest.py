"""
Keep the test suite offline, without making it fake.

Embeddings moved from an in-process model to an HTTP call, and that broke a
property the suite quietly relied on: it used to run with no network and no
credential. Left alone, 353 tests would now each make an API request,
blow through a 100-request-per-minute free tier, and fail on any machine
without a key.

The obvious fix — a random or hash-based stand-in vector — would be worse
than the problem. Tests like «كيف أجدد جواز سفري؟» assert that retrieval
finds the passport file in the REAL index. A made-up vector retrieves
nothing meaningful, so those tests would either be deleted or rewritten
into assertions that prove nothing.

So the vectors here are real, just recorded. Each test question was
embedded once by the live API and cached; the fixture replays them. Tests
stay offline and fast, and still exercise real similarity against the real
index.

--- Adding a test that embeds a new question ------------------------------

The cache is keyed by exact text, so a new question is a cache miss and
fails with instructions rather than silently returning something wrong.
Record it by running the suite once with a real key:

    EMBED_CACHE_RECORD=1 LLM_API_KEY=<real key> pytest

That appends any missing questions to query_vectors.json. Commit the file.

--- When to re-record everything ------------------------------------------

Whenever the embedding model or its dimension changes. A cache recorded
under one model is meaningless under another, so the fixture refuses to
serve vectors whose width no longer matches settings.embed_dim.
"""

import json
import os
from pathlib import Path

import numpy as np
import pytest

CACHE = Path(__file__).parent / "fixtures" / "query_vectors.json"
RECORDING = os.environ.get("EMBED_CACHE_RECORD") == "1"


def _load():
    if not CACHE.exists():
        return {}
    return json.loads(CACHE.read_text(encoding="utf-8"))


def _save(cache):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                     encoding="utf-8")


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch):
    """Serve recorded vectors instead of calling the embedding API."""
    from chatbot.config import settings
    from chatbot.rag import retrieval

    cache = _load()
    added = []

    def fake_encode(texts, prefix):
        if not texts:
            return np.zeros((0, settings.embed_dim), dtype=np.float32)

        rows = []
        for text in texts:
            hit = cache.get(text)
            if hit is not None and len(hit) == settings.embed_dim:
                rows.append(hit)
                continue
            if not RECORDING:
                raise AssertionError(
                    f"No recorded embedding for {text!r}.\n"
                    f"The suite runs offline against cached vectors. Record "
                    f"this one with:\n"
                    f"    EMBED_CACHE_RECORD=1 LLM_API_KEY=<real key> pytest\n"
                    f"then commit tests/fixtures/query_vectors.json."
                )
            # Recording: go to the real backend, once, and remember it.
            vector = retrieval._embed_gemini([text])[0]
            cache[text] = [float(x) for x in vector]
            added.append(text)
            rows.append(cache[text])

        return np.asarray(rows, dtype=np.float32)

    monkeypatch.setattr(retrieval, "_encode", fake_encode)
    yield
    if RECORDING and added:
        _save(cache)
