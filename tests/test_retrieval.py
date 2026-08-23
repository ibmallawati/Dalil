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

# Captured at import, which happens during collection — before conftest's
# autouse fixture swaps _encode for recorded vectors. The prefix and
# normalisation tests below need the real one.
_REAL_ENCODE = retrieval._encode


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
    """
    Exercise the LOCAL backend with a stand-in model.

    conftest's autouse fixture replaces _encode with recorded vectors so the
    suite stays offline. These tests are about what _encode does BEFORE it
    embeds anything — the e5 prefixes and D3 normalisation — so they put the
    real implementation back and point it at a fake model.
    """
    import dataclasses

    m = FakeModel()
    monkeypatch.setattr(retrieval, "_encode", _REAL_ENCODE)
    monkeypatch.setattr(retrieval, "settings",
                        dataclasses.replace(retrieval.settings,
                                            embed_backend="local",
                                            embed_model="fake-local-model",
                                            embed_dim=m.dim))
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


# --- The Gemini backend ---------------------------------------------------
# Every test here is a bug that actually happened while wiring this up.
# None of them raise on their own; each one silently corrupts similarity.

class FakeEmbeddings:
    """Stands in for client.embeddings, recording what it was asked for."""

    def __init__(self, dim=768, index_values=None, count=None, norm=1.0):
        self.dim = dim
        self.index_values = index_values
        self.count = count
        self.norm = norm
        self.calls = []

    def create(self, model, input, dimensions):
        self.calls.append({"model": model, "input": list(input),
                           "dimensions": dimensions})
        n = self.count if self.count is not None else len(input)
        rows = []
        for i in range(n):
            rng = np.random.default_rng(i)
            v = rng.normal(size=self.dim).astype(np.float32)
            v = v / np.linalg.norm(v) * self.norm      # control the length
            idx = self.index_values[i] if self.index_values else None
            rows.append(dataclasses.make_dataclass(
                "Row", ["embedding", "index"])(list(map(float, v)), idx))
        return dataclasses.make_dataclass("Resp", ["data"])(rows)


@pytest.fixture
def fake_api(monkeypatch):
    """Point the gemini backend at a fake client, with the real _encode."""
    def install(**kwargs):
        api = FakeEmbeddings(**kwargs)
        monkeypatch.setattr(retrieval, "_encode", _REAL_ENCODE)
        monkeypatch.setattr(
            retrieval, "settings",
            dataclasses.replace(retrieval.settings, embed_backend="gemini",
                                embed_model="gemini-embedding-001",
                                embed_dim=api.dim))
        import chatbot.llm
        monkeypatch.setattr(chatbot.llm.client, "embeddings", api,
                            raising=False)
        return api
    return install


def test_truncated_vectors_are_renormalised(fake_api):
    """
    The bug that would have broken everything quietly.

    gemini-embedding-001 is natively 3072-wide and truncates on request.
    A truncated vector is NOT unit length — measured 0.5790 at 768 dims —
    and retrieve() treats a dot product AS cosine similarity. Skipping the
    re-normalisation rescales every score by ~0.58 and invalidates every
    threshold tuned against the old scale, with no error anywhere.
    """
    fake_api(norm=0.579)
    v = retrieval.embed_query("كم الرسوم؟")
    assert np.isclose(np.linalg.norm(v), 1.0, atol=1e-5)


def test_e5_prefixes_are_not_sent_to_gemini(fake_api):
    """
    "query: " and "passage: " are e5 training artefacts. Gemini was not
    trained with them, so sending them embeds the literal English word
    "query" into every Arabic question.
    """
    api = fake_api()
    retrieval.embed_query("كيف أجدد جواز سفري؟")
    sent = api.calls[0]["input"][0]
    assert not sent.startswith(QUERY_PREFIX)
    assert "query" not in sent and "passage" not in sent


def test_d3_normalisation_still_runs(fake_api):
    """Changing backend must not drop the Arabic normalisation step."""
    api = fake_api()
    retrieval.embed_query("جـــواز سَفر ٥٠٠")
    sent = api.calls[0]["input"][0]
    assert "ـ" not in sent and "٥" not in sent and "500" in sent


def test_requested_width_is_the_configured_one(fake_api):
    api = fake_api()
    retrieval.embed_query("اختبار")
    assert api.calls[0]["dimensions"] == retrieval.settings.embed_dim


def test_a_wrong_width_response_is_rejected(fake_api, monkeypatch):
    """
    A model that ignores `dimensions` would produce vectors the index
    cannot be compared against. Better to fail loudly than to let NumPy
    raise something opaque later, or worse, silently broadcast.
    """
    fake_api(dim=1536)                      # the API returns 1536...
    monkeypatch.setattr(                    # ...but the index is 768 wide
        retrieval, "settings",
        dataclasses.replace(retrieval.settings, embed_dim=768))
    with pytest.raises(RuntimeError, match="expected 768"):
        retrieval.embed_query("اختبار")


def test_missing_index_field_falls_back_to_response_order(fake_api):
    """
    Gemini's OpenAI-compatibility layer returns index=None and relies on
    response order. Sorting by index crashed the first index build.
    """
    api = fake_api(index_values=[None, None, None])
    out = retrieval.embed_passages(["أ", "ب", "ج"])
    assert out.shape == (3, api.dim)


def test_a_short_response_is_refused(fake_api):
    """
    Fewer vectors than texts means row N is no longer text N. Accepting it
    would misalign the whole index — every chunk paired with someone else's
    vector, and no error until the answers made no sense.
    """
    fake_api(count=2)
    with pytest.raises(RuntimeError, match="Refusing"):
        retrieval.embed_passages(["أ", "ب", "ج"])


def test_large_input_is_split_into_batches(fake_api):
    """
    build_index.py hands over the whole corpus in one call. The free tier
    caps a request, so oversized input must be split rather than rejected.
    """
    api = fake_api()
    out = retrieval.embed_passages([f"نص {i}" for i in range(250)])
    assert out.shape == (250, api.dim)
    assert len(api.calls) == 3          # 100 + 100 + 50
    assert all(np.isclose(np.linalg.norm(r), 1.0, atol=1e-5) for r in out)


def test_empty_input_makes_no_api_call(fake_api):
    api = fake_api()
    assert retrieval.embed_passages([]).shape == (0, api.dim)
    assert api.calls == []
