"""
Retrieval tests — the E5 contract and the stale-index tripwire.

These use a FAKE embedding model so they run in milliseconds with no
weights download. They are not testing whether e5 is a good model; they are
testing the wiring around it, which is where the silent failures live:

  - the "query: " / "passage: " prefixes actually reaching the encoder,
  - D3 normalization running before encoding, on both sides,
  - a stale index being rejected rather than scored.

Real Arabic semantic quality is measured separately, by the D7 evaluation
set, which is the honest place for it.
"""

import dataclasses

import numpy as np
import pytest

from chatbot.rag import retrieval
from chatbot.rag.retrieval import PASSAGE_PREFIX, QUERY_PREFIX


class FakeModel:
    """Records what it was asked to encode, returns deterministic vectors."""

    def __init__(self, dim=768):
        self.seen = []
        self.dim = dim

    def get_sentence_embedding_dimension(self):
        return self.dim

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        self.seen.extend(texts)
        # Deterministic pseudo-vectors: identical text -> identical vector.
        out = []
        for t in texts:
            rng = np.random.default_rng(abs(hash(t)) % (2**32))
            v = rng.normal(size=self.dim).astype(np.float32)
            if normalize_embeddings:
                v /= np.linalg.norm(v)
            out.append(v)
        return np.stack(out)


@pytest.fixture
def fake_model(monkeypatch):
    m = FakeModel()
    monkeypatch.setattr(retrieval, "_model", m)
    return m


# --- The E5 prefix contract ----------------------------------------------
# This is the highest-value test in the file. Omitting an e5 prefix raises
# nothing and breaks nothing visibly -- it just quietly costs retrieval
# quality, and it would do so AFTER D2 set a threshold and D7 published an
# accuracy number, corrupting both with no error anywhere.

def test_query_gets_query_prefix(fake_model):
    retrieval.embed_query("كيف أجدد جواز سفري؟")
    assert len(fake_model.seen) == 1
    assert fake_model.seen[0].startswith(QUERY_PREFIX)


def test_passages_get_passage_prefix(fake_model):
    retrieval.embed_passages(["تجديد جواز السفر", "تجديد البطاقة الشخصية"])
    assert len(fake_model.seen) == 2
    assert all(t.startswith(PASSAGE_PREFIX) for t in fake_model.seen)


def test_query_and_passage_prefixes_differ(fake_model):
    """e5 is asymmetric: the two sides must NOT be embedded identically."""
    retrieval.embed_query("تجديد جواز السفر")
    retrieval.embed_passages(["تجديد جواز السفر"])
    assert fake_model.seen[0] != fake_model.seen[1]


# --- D3 normalization runs on both sides ---------------------------------

def test_normalization_applied_before_embedding(fake_model):
    retrieval.embed_query("جـــواز سَفر ٥٠٠")
    sent = fake_model.seen[0][len(QUERY_PREFIX):]
    assert "ـ" not in sent          # tatweel gone
    assert "٥" not in sent          # Arabic-Indic digits folded
    assert "500" in sent


def test_both_paths_normalize_identically(fake_model):
    """
    The D3 guarantee at the embedding boundary: a document spelling and a
    citizen spelling must reach the encoder as the same string (modulo the
    prefix). If this ever fails, retrieval degrades with no error.
    """
    retrieval.embed_query("تجديد جَواز السَفر")
    retrieval.embed_passages(["تجديد جواز السفر"])
    q = fake_model.seen[0][len(QUERY_PREFIX):]
    p = fake_model.seen[1][len(PASSAGE_PREFIX):]
    assert q == p


def test_identical_text_gives_identical_vector(fake_model):
    a = retrieval.embed_query("تجديد جواز السفر")
    b = retrieval.embed_query("تجديد جـواز السَفر")   # same after normalization
    assert np.allclose(a, b)


def test_embeddings_are_unit_length(fake_model):
    """retrieve() uses a bare dot product, which IS cosine only if unit."""
    v = retrieval.embed_query("اختبار")
    assert np.isclose(np.linalg.norm(v), 1.0, atol=1e-5)


# --- Threshold + index guards --------------------------------------------

def _install_index(monkeypatch, chunks, dim=768, seed=0):
    rng = np.random.default_rng(seed)
    vecs = rng.normal(size=(len(chunks), dim)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    monkeypatch.setattr(retrieval, "_doc_embeddings", vecs)
    monkeypatch.setattr(retrieval, "KNOWLEDGE", chunks)
    return vecs


def _set_threshold(monkeypatch, value):
    """
    Settings is a frozen dataclass (deliberately — config should not be
    mutable at runtime), so swap in a modified copy rather than assigning
    to the field.
    """
    monkeypatch.setattr(
        retrieval, "settings",
        dataclasses.replace(retrieval.settings, similarity_threshold=value),
    )


def test_returns_empty_when_no_index(monkeypatch, fake_model):
    monkeypatch.setattr(retrieval, "_doc_embeddings", None)
    assert retrieval.retrieve("أي سؤال") == []


def test_threshold_filters_everything_out(monkeypatch, fake_model):
    """
    An impossible threshold must yield [] -- that empty list is what drives
    the D6 refusal paths, so it is a meaningful result, not a failure.
    """
    _install_index(monkeypatch, [{"source": "a.md", "text": "x"}])
    _set_threshold(monkeypatch, 1.01)
    assert retrieval.retrieve("سؤال") == []


def test_results_carry_score_and_chunk_id(monkeypatch, fake_model):
    _install_index(monkeypatch, [{"source": "a.md", "text": "x"},
                                 {"source": "b.md", "text": "y"}])
    _set_threshold(monkeypatch, -1.0)
    results = retrieval.retrieve("سؤال", k=2)
    assert len(results) == 2
    for r in results:
        assert "score" in r and "chunk_id" in r and "source" in r
    # Sorted best-first.
    assert results[0]["score"] >= results[1]["score"]
