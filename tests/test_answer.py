"""
Generation tests — parsing, fallback, and the provenance guarantee.

No network. The model is faked so the failure modes that matter can be
produced on demand: fenced JSON, JSON wrapped in prose, truncated output,
outright garbage, and a model that tries to supply its own citation.

The answer text itself is now free-form, so these tests assert on the
CONTRACT around it — that something readable always comes back, that the
confidence verdict is honoured, and that sources are never the model's.
"""

import json

import pytest

from chatbot.agent import answer as answer_mod
from chatbot.agent.answer import (
    NO_CONTEXT_REPLY,
    build_context,
    generate_answer,
    parse_answer,
)


def chunk(section="الرسوم", text="عشرون ريالاً عمانياً",
          service="تجديد جواز السفر", url="https://www.rop.gov.om/passport"):
    return {
        "text": text, "section": section, "service": service,
        "service_id": "rop-passport-renewal",
        "authority": "شرطة عمان السلطانية",
        "source_url": url, "source_title": "تجديد جواز السفر",
    }


CHUNKS = [chunk()]
GOOD = {"answer": "رسوم تجديد الجواز عشرون ريالاً عمانياً.", "confidence": "high"}


class FakeCompletion:
    def __init__(self, content, finish_reason="stop"):
        self.model = "fake-model"
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": content})(),
            "finish_reason": finish_reason,
        })()]
        self.usage = type("U", (), {
            "prompt_tokens": 100, "completion_tokens": 60, "total_tokens": 160,
        })()


def fake_chat(monkeypatch, content, finish_reason="stop"):
    monkeypatch.setattr(answer_mod, "chat",
                        lambda *a, **k: FakeCompletion(content, finish_reason))


# --- parsing recovers responses that are correct but wrapped -------------

def test_parses_raw_json():
    assert parse_answer(json.dumps(GOOD, ensure_ascii=False))["confidence"] == "high"


def test_parses_fenced_json():
    fenced = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert parse_answer(fenced)["answer"] == GOOD["answer"]


def test_parses_json_embedded_in_prose():
    noisy = "إليك الإجابة:\n" + json.dumps(GOOD, ensure_ascii=False) + "\nانتهى."
    assert parse_answer(noisy)["answer"] == GOOD["answer"]


@pytest.mark.parametrize("bad", ["", None, "لا يوجد JSON هنا", "{broken", "[1,2,3]"])
def test_unparseable_returns_none(bad):
    assert parse_answer(bad) is None


def test_json_without_an_answer_key_is_not_accepted():
    """A well-formed object with no answer is not an answer."""
    assert parse_answer('{"confidence": "high"}') is None


# --- the screen is never blank -------------------------------------------

def test_malformed_response_still_returns_the_text_with_its_sources(monkeypatch):
    fake_chat(monkeypatch, "رسوم التجديد عشرون ريالاً عمانياً.")
    result = generate_answer("كم الرسوم؟", CHUNKS)
    assert "عشرون" in result["answer"]
    assert result["source_url"] == "https://www.rop.gov.om/passport"


def test_empty_model_reply_still_returns_something(monkeypatch):
    fake_chat(monkeypatch, "")
    assert generate_answer("سؤال", CHUNKS)["answer"]


def test_no_chunks_returns_an_honest_reply_without_calling_the_model(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not call the model with no grounding")
    monkeypatch.setattr(answer_mod, "chat", explode)
    result = generate_answer("سؤال", [])
    assert result["answer"] == NO_CONTEXT_REPLY
    assert result["confidence"] == "none"


# --- the provenance guarantee --------------------------------------------

def test_model_cannot_choose_its_own_citation(monkeypatch):
    """
    A fabricated citation survives a glance in a way a fabricated fact does
    not, so provenance is overwritten from chunk metadata after generation.
    """
    fake_chat(monkeypatch, json.dumps(
        {**GOOD, "source_url": "https://invented.example/x"}, ensure_ascii=False))
    assert generate_answer("سؤال", CHUNKS)["source_url"] == \
        "https://www.rop.gov.om/passport"


def test_answer_spanning_two_services_cites_both(monkeypatch):
    fake_chat(monkeypatch, json.dumps({**GOOD, "used": [1, 2]},
                                      ensure_ascii=False))
    chunks = [
        chunk(service="تجديد جواز السفر", url="https://www.rop.gov.om/passport"),
        chunk(service="تجديد رخصة السياقة", url="https://www.rop.gov.om/licence"),
    ]
    assert len(generate_answer("سؤال", chunks)["sources"]) == 2


# --- citing what was USED, not what was retrieved ------------------------
# Retrieval widened when L1 stopped bypassing it. Live before this: a
# passport+licence question retrieved five services and cited all five,
# putting «تجديد رخصة مرشد سياحي» under an answer about passports.

def test_only_the_passages_the_model_used_are_cited(monkeypatch):
    fake_chat(monkeypatch, json.dumps({**GOOD, "used": [1]}, ensure_ascii=False))
    chunks = [
        chunk(service="تجديد جواز السفر", url="https://www.rop.gov.om/passport"),
        chunk(service="تجديد رخصة مرشد سياحي", url="https://mht.gov.om/guide"),
    ]
    sources = generate_answer("كم رسوم تجديد الجواز؟", chunks)["sources"]
    assert [s["service"] for s in sources] == ["تجديد جواز السفر"]


def test_the_primary_source_follows_the_selection(monkeypatch):
    """
    metadata_from reads source_url off the FIRST cited chunk, so narrowing
    has to move the headline citation too — not just shorten the list.
    """
    fake_chat(monkeypatch, json.dumps({**GOOD, "used": [2]}, ensure_ascii=False))
    chunks = [
        chunk(service="تجديد رخصة مرشد سياحي", url="https://mht.gov.om/guide"),
        chunk(service="تجديد جواز السفر", url="https://www.rop.gov.om/passport"),
    ]
    assert generate_answer("سؤال", chunks)["source_url"] == \
        "https://www.rop.gov.om/passport"


@pytest.mark.parametrize("used", [None, [], "1,2", ["x"], [99], 3])
def test_an_unusable_selection_falls_back_to_tracing(monkeypatch, used):
    """
    Every malformed shape a model can emit degrades to text tracing rather
    than to an exception. Both chunks here are quoted by the answer, so both
    are correctly recovered.
    """
    fake_chat(monkeypatch, json.dumps({**GOOD, "used": used}, ensure_ascii=False))
    chunks = [
        chunk(service="تجديد جواز السفر", url="https://www.rop.gov.om/passport"),
        chunk(service="تجديد رخصة السياقة", url="https://www.rop.gov.om/licence"),
    ]
    assert len(generate_answer("سؤال", chunks)["sources"]) == 2


def test_a_missing_used_list_does_not_cite_unrelated_services(monkeypatch):
    """
    The regression this fallback exists for. Models omit `used` — observed
    on 2 of 4 turns in one live conversation, always the short answers — and
    "cite everything" then listed eight ministries under a one-line fee,
    «التقارير والمؤشرات الاقتصادية» among them, beneath an answer about
    passport documents.

    A citation beside a government fee is a claim about where that fee came
    from. Eight unrelated ministries is a false claim, not a generous one.
    """
    fake_chat(monkeypatch, json.dumps(
        {"answer": "رسوم تجديد الجواز عشرون ريالاً عمانياً.",
         "confidence": "high"}, ensure_ascii=False))       # no "used" at all
    chunks = [
        chunk(service="التقارير والمؤشرات الاقتصادية", text="بيانات اقتصادية",
              url="https://moec.example/x"),
        chunk(service="طلب أرض سكنية", text="تخصيص قطعة أرض سكنية",
              url="https://mohup.example/x"),
        chunk(service="تجديد جواز السفر", text="عشرون ريالاً عمانياً",
              url="https://www.rop.gov.om/passport"),
    ]
    sources = generate_answer("كم الرسوم؟", chunks)["sources"]
    assert [s["service"] for s in sources] == ["تجديد جواز السفر"]


def test_an_untraceable_answer_cites_the_top_hit_only(monkeypatch, caplog):
    """
    Nothing reported and nothing quoted. The best-matching passage is the
    honest single guess; listing the rest would invent agreement between
    sources that were never consulted.
    """
    fake_chat(monkeypatch, json.dumps(
        {"answer": "لا تتوفر تفاصيل كافية.", "confidence": "partial"},
        ensure_ascii=False))
    chunks = [
        chunk(service="أ", text="نص مختلف تماماً", url="https://a.gov.om"),
        chunk(service="ب", text="نص آخر مختلف", url="https://b.gov.om"),
    ]
    with caplog.at_level("WARNING"):
        sources = generate_answer("سؤال", chunks)["sources"]
    assert [s["service"] for s in sources] == ["أ"]
    assert "answer_citations_unresolved" in caplog.text


def test_an_overlong_used_list_is_cut_down_by_tracing(monkeypatch):
    """
    The self-report fails in BOTH directions. On a re-run of the same
    conversation the model listed nearly every passage it had read, putting
    «التقارير والمؤشرات الاقتصادية» under an answer about passport fees.

    Where the model claims a passage the text does not support, the text
    wins — it is evidence rather than a report.
    """
    fake_chat(monkeypatch, json.dumps(
        {"answer": "رسوم تجديد الجواز عشرون ريالاً عمانياً.",
         "confidence": "high", "used": [1, 2, 3]}, ensure_ascii=False))
    chunks = [
        chunk(service="التقارير والمؤشرات الاقتصادية", text="بيانات اقتصادية",
              url="https://moec.example/x"),
        chunk(service="طلب أرض سكنية", text="تخصيص قطعة أرض سكنية",
              url="https://mohup.example/x"),
        chunk(service="تجديد جواز السفر", text="عشرون ريالاً عمانياً",
              url="https://www.rop.gov.om/passport"),
    ]
    sources = generate_answer("كم الرسوم؟", chunks)["sources"]
    assert [s["service"] for s in sources] == ["تجديد جواز السفر"]


def test_a_paraphrased_answer_still_cites_what_the_model_reported(monkeypatch):
    """
    Tracing needs verbatim overlap, and not every answer has it. When the
    text proves nothing, the self-report is the only signal left and is
    better than guessing at the top hit.
    """
    fake_chat(monkeypatch, json.dumps(
        {"answer": "الرسوم بسيطة نسبياً.", "confidence": "partial", "used": [2]},
        ensure_ascii=False))
    chunks = [
        chunk(service="أ", text="نص غير مقتبس", url="https://a.gov.om"),
        chunk(service="ب", text="نص آخر غير مقتبس", url="https://b.gov.om"),
    ]
    assert [s["service"] for s in generate_answer("س", chunks)["sources"]] == ["ب"]


def test_tracing_needs_a_real_run_of_words_not_one_shared_token(monkeypatch):
    """
    «الرسوم» appears in half the corpus. Matching on a single common word
    would trace an answer to every service that mentions fees, which is the
    over-citation this replaced.
    """
    from chatbot.agent.answer import traced_chunks

    answer = "رسوم تجديد الجواز عشرون ريالاً عمانياً."
    assert traced_chunks([chunk(text="الرسوم تختلف حسب نوع المركبة")], answer) == []
    assert len(traced_chunks([chunk(text="عشرون ريالاً عمانياً")], answer)) == 1


def test_string_indices_are_accepted(monkeypatch):
    """Models emit ["1"] as readily as [1]; both mean the same passage."""
    fake_chat(monkeypatch, json.dumps({**GOOD, "used": ["1"]}, ensure_ascii=False))
    chunks = [
        chunk(service="تجديد جواز السفر", url="https://www.rop.gov.om/passport"),
        chunk(service="تجديد رخصة السياقة", url="https://www.rop.gov.om/licence"),
    ]
    assert len(generate_answer("سؤال", chunks)["sources"]) == 1


def test_selecting_a_passage_cannot_invent_a_source(monkeypatch):
    """
    The model picks AMONG the passages it was given. It has no way to add
    one, so narrowing the citation list does not hand it provenance — the
    URL still comes from chunk metadata.
    """
    fake_chat(monkeypatch, json.dumps(
        {**GOOD, "used": [1], "source_url": "https://invented.example/x"},
        ensure_ascii=False))
    assert generate_answer("سؤال", CHUNKS)["source_url"] == \
        "https://www.rop.gov.om/passport"


def test_context_numbers_each_passage(monkeypatch):
    """The numbers are what `used` refers to, so they must be in the prompt."""
    context = build_context([chunk(section="الرسوم"), chunk(section="الخطوات")])
    assert "[1]" in context and "[2]" in context


# --- L3 confidence --------------------------------------------------------

def test_bad_confidence_from_the_model_degrades_to_none(monkeypatch):
    fake_chat(monkeypatch, json.dumps({**GOOD, "confidence": "مؤكد"},
                                      ensure_ascii=False))
    assert generate_answer("سؤال", CHUNKS)["confidence"] == "none"


def test_l3_none_is_preserved(monkeypatch):
    """
    When the model reports it has no grounds, that verdict must survive —
    app.py turns it into a refusal rather than presenting the text.
    """
    fake_chat(monkeypatch, json.dumps(
        {"answer": "لا تتوفر معلومات", "confidence": "none"}, ensure_ascii=False))
    assert generate_answer("سؤال خارج النطاق", CHUNKS)["confidence"] == "none"


# --- truncation is named, not mysterious ---------------------------------

def test_truncated_reply_is_logged_as_truncation(monkeypatch, caplog):
    fake_chat(monkeypatch, '{"answer": "الرسوم', finish_reason="length")
    with caplog.at_level("WARNING"):
        result = generate_answer("سؤال", CHUNKS)
    assert "answer_truncated" in caplog.text
    assert result["answer"]


# --- context assembly ----------------------------------------------------

def test_context_labels_each_passage_with_its_service():
    """
    An answer can span services, so the model must be able to tell which
    passage came from which — otherwise it cannot attribute correctly.
    """
    context = build_context([
        chunk(service="تجديد جواز السفر", section="الرسوم"),
        chunk(service="تجديد رخصة السياقة", section="الرسوم"),
    ])
    assert "تجديد جواز السفر" in context
    assert "تجديد رخصة السياقة" in context


def test_history_is_passed_but_grounding_is_separate(monkeypatch):
    captured = {}

    def capture(messages, **kw):
        captured["messages"] = messages
        return FakeCompletion(json.dumps(GOOD, ensure_ascii=False))

    monkeypatch.setattr(answer_mod, "chat", capture)
    generate_answer("كم الرسوم؟", CHUNKS,
                    history=[{"role": "user", "content": "سؤال سابق"}])
    assert [m["role"] for m in captured["messages"]] == ["system", "user", "user"]
    assert "النصوص المتاحة" in captured["messages"][0]["content"]
