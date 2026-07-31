"""
D5 + L3 tests — parsing, fallback, and the anti-hallucination post-pass.

No network. The model is faked so the failure modes that matter can be
produced on demand: fenced JSON, JSON wrapped in prose, truncated JSON,
outright garbage, and a model that ignores the prompt and invents a fee.

The last one is the point of the file.
"""

import json

import pytest

from chatbot.agent import answer
from chatbot.agent.answer import (
    build_context,
    generate_answer,
    parse_card,
    prose_fallback,
)
from chatbot.agent.card import CARD_FIELDS, NOT_IN_SOURCE


def chunk(section, text="نص"):
    return {
        "section": section, "text": text,
        "service": "تجديد جواز السفر", "service_id": "rop-passport-renewal",
        "authority": "شرطة عمان السلطانية",
        "source_url": "https://www.rop.gov.om/passport",
        "source_title": "تجديد جواز السفر",
    }


FULL = [chunk(f.section_ar) for f in CARD_FIELDS]
NO_FEES = [chunk(f.section_ar) for f in CARD_FIELDS if f.section_ar != "الرسوم"]

GOOD_JSON = {
    "eligibility": "المواطنون العمانيون",
    "required_documents": ["البطاقة الشخصية", "الجواز القديم"],
    "steps": ["الدخول إلى التطبيق", "سداد الرسوم"],
    "fees": "عشرون ريالاً عمانياً",
    "estimated_time": "ثلاثة أيام عمل",
    "where": "إلكترونياً أو في مراكز الخدمة",
    "confidence": "high",
}


class FakeCompletion:
    def __init__(self, content, finish_reason="stop"):
        self.model = "fake-model"
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": content})(),
            "finish_reason": finish_reason,
        })()]
        self.usage = type("U", (), {
            "prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300,
        })()


def fake_chat(monkeypatch, content, finish_reason="stop"):
    monkeypatch.setattr(answer, "chat",
                        lambda *a, **k: FakeCompletion(content, finish_reason))


# --- Parsing recovers responses that are correct but wrapped -------------

def test_parses_raw_json():
    assert parse_card(json.dumps(GOOD_JSON))["fees"] == "عشرون ريالاً عمانياً"


def test_parses_fenced_json():
    """Models emit ```json fences despite being told not to."""
    fenced = "```json\n" + json.dumps(GOOD_JSON, ensure_ascii=False) + "\n```"
    assert parse_card(fenced)["confidence"] == "high"


def test_parses_json_embedded_in_prose():
    noisy = "إليك الإجابة:\n" + json.dumps(GOOD_JSON, ensure_ascii=False) + "\nأتمنى أن يفيدك."
    assert parse_card(noisy)["fees"] == "عشرون ريالاً عمانياً"


@pytest.mark.parametrize("bad", ["", None, "لا يوجد JSON هنا", "{broken", "[1,2,3]"])
def test_unparseable_returns_none(bad):
    assert parse_card(bad) is None


# --- The screen is never blank -------------------------------------------

def test_malformed_response_degrades_to_prose_with_its_citation(monkeypatch):
    """
    D5: a malformed JSON response must never blank the screen mid-demo. The
    Arabic text is still useful, so it is shown as prose — with the real
    source, which comes from the corpus rather than the model.
    """
    fake_chat(monkeypatch, "الرسوم عشرون ريالاً عمانياً، ويمكن التجديد إلكترونياً.")
    result = generate_answer("كم الرسوم؟", FULL)
    assert result["format"] == "prose"
    assert "عشرون" in result["text"]
    assert result["source_url"] == "https://www.rop.gov.om/passport"


def test_prose_fallback_strips_code_fences():
    out = prose_fallback("```\nنص الإجابة\n```", FULL)
    assert out["text"] == "نص الإجابة"


def test_empty_model_reply_still_returns_something(monkeypatch):
    fake_chat(monkeypatch, "")
    result = generate_answer("سؤال", FULL)
    assert result["text"]


def test_no_chunks_returns_a_fully_marked_card_without_calling_the_model(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not call the model with no grounding")
    monkeypatch.setattr(answer, "chat", explode)
    result = generate_answer("سؤال", [])
    assert result["confidence"] == "none"
    assert result["fees"] == NOT_IN_SOURCE


# --- The post-pass: the guarantee ---------------------------------------

def test_invented_fee_is_overwritten_when_the_section_is_absent(monkeypatch):
    """
    THE test in this file.

    The context has no «الرسوم» section. The model ignores the prompt and
    supplies a confident fee anyway. Without the post-pass this reaches the
    citizen as an invented government fee underneath a real ROP source URL
    — a fabrication the citation makes look verified.
    """
    invented = dict(GOOD_JSON, fees="خمسون ريالاً عمانياً")
    fake_chat(monkeypatch, json.dumps(invented, ensure_ascii=False))
    result = generate_answer("كم الرسوم؟", NO_FEES)
    assert result["fees"] == NOT_IN_SOURCE
    # Fields the context DOES support survive untouched.
    assert result["steps"] == ["الدخول إلى التطبيق", "سداد الرسوم"]


def test_model_cannot_choose_its_own_citation(monkeypatch):
    fake_chat(monkeypatch, json.dumps(
        dict(GOOD_JSON, source_url="https://invented.example/x"), ensure_ascii=False))
    result = generate_answer("سؤال", FULL)
    assert result["source_url"] == "https://www.rop.gov.om/passport"


def test_bad_confidence_from_the_model_degrades_to_none(monkeypatch):
    fake_chat(monkeypatch, json.dumps(dict(GOOD_JSON, confidence="مؤكد"),
                                      ensure_ascii=False))
    assert generate_answer("سؤال", FULL)["confidence"] == "none"


def test_l3_none_is_preserved(monkeypatch):
    """
    When the model correctly reports it has no grounds, that verdict must
    survive to the UI — it is what triggers the refusal rendering instead
    of a checklist.
    """
    fake_chat(monkeypatch, json.dumps(
        {"confidence": "none", **{f.key: NOT_IN_SOURCE for f in CARD_FIELDS}},
        ensure_ascii=False))
    result = generate_answer("سؤال خارج النطاق", FULL)
    assert result["confidence"] == "none"


# --- Truncation is named, not mysterious ---------------------------------

def test_truncated_reply_is_logged_as_truncation(monkeypatch, caplog):
    """
    Measured: a long service needs ~832 completion tokens, and at
    max_tokens=500 the JSON does not parse. That must surface as
    'truncated', not as an unexplained parse failure.
    """
    fake_chat(monkeypatch, '{"eligibility": "المواطنون', finish_reason="length")
    with caplog.at_level("WARNING"):
        result = generate_answer("سؤال", FULL)
    assert "answer_truncated" in caplog.text
    assert result["format"] == "prose"


def test_blocked_hallucination_is_logged(monkeypatch, caplog):
    """Evidence the layer earns its place, visible in the logs."""
    fake_chat(monkeypatch, json.dumps(dict(GOOD_JSON, fees="خمسون ريالاً"),
                                      ensure_ascii=False))
    with caplog.at_level("WARNING"):
        generate_answer("كم الرسوم؟", NO_FEES)
    assert "ungrounded_fields_overwritten" in caplog.text


# --- Context assembly ----------------------------------------------------

def test_context_carries_authority_and_service():
    context = build_context(FULL)
    assert "شرطة عمان السلطانية" in context
    assert "تجديد جواز السفر" in context
    assert "## الرسوم" in context


def test_history_is_included_but_grounding_is_not_taken_from_it(monkeypatch):
    """
    Prior turns give the model the thread (e.g. the answer to a clarifying
    question). The system prompt is explicit that only «المعلومات» may be
    used as grounding.
    """
    captured = {}

    def capture(messages, **kw):
        captured["messages"] = messages
        return FakeCompletion(json.dumps(GOOD_JSON, ensure_ascii=False))

    monkeypatch.setattr(answer, "chat", capture)
    generate_answer("كم الرسوم؟", FULL,
                    history=[{"role": "user", "content": "سؤال سابق"}])
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user", "user"]
    assert "المعلومات" in captured["messages"][0]["content"]
