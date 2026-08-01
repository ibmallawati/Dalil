"""
Router tests — retrieval strategy follows from what L1 resolved.

The behaviour that matters here is NEGATIVE: when L1 has resolved a
service, similarity search must not run. Top-k over a resolved service is
how an eight-field answer card gets filled from the model's general
knowledge instead of from the corpus, which is how an invented fee ends up
underneath a real government citation.

So these tests assert on which retrieval function was CALLED, not only on
what came back.
"""

import pytest

from chatbot.agent import router
from chatbot.agent.registry import Verdict
from chatbot.agent.router import Strategy, plan_and_gather


@pytest.fixture
def spy(monkeypatch):
    """Replace both retrieval paths so we can see which one ran."""
    calls = {"fetch_service": [], "retrieve": []}

    def fake_fetch_service(service_id):
        calls["fetch_service"].append(service_id)
        return [
            {"service_id": service_id, "section": s, "text": f"نص {s}",
             "source_url": "https://example.om/x", "chunk_id": i, "score": None}
            for i, s in enumerate(["الفئة المستهدفة", "المستندات المطلوبة",
                                   "الخطوات", "الرسوم", "المدة المتوقعة",
                                   "أين تتم المعاملة", "ملاحظات"])
        ]

    def fake_retrieve(question, k=3):
        calls["retrieve"].append(question)
        return [{"service_id": "x", "section": "قسم", "text": "نص",
                 "source_url": "https://example.om/x", "chunk_id": 0, "score": 0.83}]

    monkeypatch.setattr(router, "fetch_service", fake_fetch_service)
    monkeypatch.setattr(router, "retrieve", fake_retrieve)
    return calls


# --- WHOLE_SERVICE: the main road ----------------------------------------

def test_resolved_service_loads_every_section_without_similarity(spy):
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.strategy is Strategy.WHOLE_SERVICE
    assert spy["fetch_service"] == ["rop-passport-renewal"]
    assert spy["retrieve"] == []          # the point of the whole change
    assert len(plan.chunks) == 7


def test_whole_service_chunks_have_no_score(spy):
    """
    No similarity was computed, so there is no score to report. A None here
    is honest; a fabricated 1.0 would misrepresent how the answer was found.
    """
    plan = plan_and_gather("ما هي رسوم تجديد البطاقة الشخصية؟")
    assert all(c["score"] is None for c in plan.chunks)


def test_whole_service_preserves_document_order(spy):
    """Sections come back as the curator wrote them: the reading order."""
    plan = plan_and_gather("متى تنتهي رخصة القيادة؟")
    sections = [c["section"] for c in plan.chunks]
    assert sections[0] == "الفئة المستهدفة"
    assert sections.index("المستندات المطلوبة") < sections.index("الرسوم")


# --- CLARIFY: deterministic, no LLM, no embedding ------------------------

def test_ambiguous_passport_question_asks_instead_of_guessing(spy):
    plan = plan_and_gather("أريد جواز سفر")
    assert plan.strategy is Strategy.CLARIFY
    assert spy["fetch_service"] == [] and spy["retrieve"] == []
    assert plan.match.clarify_question_ar
    assert len(plan.match.clarify_options) == 2
    ids = {o["service_id"] for o in plan.match.clarify_options}
    assert ids == {"rop-passport-renewal", "rop-passport-new-issuance"}


def test_clarify_options_carry_tappable_labels(spy):
    """D4 renders these as buttons, so each option needs Arabic label text."""
    plan = plan_and_gather("أريد جواز سفر")
    for option in plan.match.clarify_options:
        assert option["label_ar"].strip()


@pytest.mark.parametrize("question,expected_service", [
    ("أريد تسجيل سجل تجاري جديد", "mociip-commercial-registration"),
    ("كيف أؤسس سجل تجاري؟", "mociip-commercial-registration"),
    ("أريد تجديد الرخصة التجارية القائمة", "mociip-licence-renewal"),
])
def test_disambiguators_resolve_without_asking(spy, question, expected_service):
    """
    Asking when the citizen already told you is as bad as guessing when they
    didn't. Max one clarifying turn only helps if the turn is necessary.
    """
    plan = plan_and_gather(question)
    assert plan.strategy is Strategy.WHOLE_SERVICE
    assert spy["fetch_service"] == [expected_service]


def test_disambiguator_does_not_fire_inside_a_longer_word(spy):
    """
    «جديد» (new) sits inside «تجديد» (renewal). Substring matching would
    read "I want to RENEW my passport" as "I want a NEW passport" — exactly
    backwards, and it would send a citizen down the wrong procedure with a
    real government citation attached.

    Word-boundary matching on disambiguators is what prevents it.
    """
    plan = plan_and_gather("أريد تجديد جواز سفري")
    assert plan.strategy is Strategy.WHOLE_SERVICE
    assert spy["fetch_service"] == ["rop-passport-renewal"]


# --- SIMILARITY: the fallback, not the main road -------------------------

def test_authority_only_question_uses_similarity(spy):
    plan = plan_and_gather("ما هي خدمات شرطة عمان السلطانية؟")
    assert plan.strategy is Strategy.SIMILARITY
    assert spy["retrieve"] and spy["fetch_service"] == []


def test_unknown_question_falls_through_to_similarity(spy):
    plan = plan_and_gather("كيف أحصل على وثيقة رسمية؟")
    assert plan.match.verdict is Verdict.UNKNOWN
    assert plan.strategy is Strategy.SIMILARITY


def test_similarity_returning_nothing_becomes_a_refusal(spy, monkeypatch):
    monkeypatch.setattr(router, "retrieve", lambda q, k=3: [])
    plan = plan_and_gather("ما هي أفضل مطاعم مسقط؟")
    assert plan.strategy is Strategy.REFUSE_NO_CONTENT
    assert not plan.grounded


# --- Refusals ------------------------------------------------------------

def test_uncovered_authority_refuses_without_touching_retrieval(spy):
    plan = plan_and_gather("كيف أحجز موعد في وزارة الصحة؟")
    assert plan.strategy is Strategy.REFUSE_UNCOVERED
    assert plan.is_refusal
    assert spy["fetch_service"] == [] and spy["retrieve"] == []


def test_registry_covers_a_service_the_corpus_lacks(spy, monkeypatch):
    """
    The registry and the corpus can disagree. When they do, Daleel must
    refuse (D6 path 1) rather than answer from nothing.
    """
    monkeypatch.setattr(router, "fetch_service", lambda service_id: [])
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.strategy is Strategy.REFUSE_NO_CONTENT
    assert not plan.grounded


# --- Verdict precedence --------------------------------------------------

def test_covered_service_beats_an_uncovered_authority_in_the_same_question(spy):
    """
    «أريد تجديد جواز سفري ثم مراجعة وزارة الصحة» asks for something Daleel
    HAS. Refusing the whole question because a second, uncovered authority
    was mentioned would withhold an answer it could give.
    """
    plan = plan_and_gather("أريد تجديد جواز سفري ثم مراجعة وزارة الصحة")
    assert plan.match.verdict is Verdict.COVERED
    assert plan.strategy is Strategy.WHOLE_SERVICE
    assert spy["fetch_service"] == ["rop-passport-renewal"]


def test_the_uncovered_authority_is_still_recorded_for_a_footnote(spy):
    """
    Covered wins, but the citizen did ask about the other authority. The
    answer should be able to say so rather than silently ignore half the
    question.
    """
    plan = plan_and_gather("أريد تجديد جواز سفري ثم مراجعة وزارة الصحة")
    assert plan.match.also_mentioned_uncovered_ar == "وزارة الصحة"


def test_no_footnote_when_nothing_uncovered_was_mentioned(spy):
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.match.also_mentioned_uncovered_ar is None


# --- Margin-based ambiguity: the net for UNDECLARED ambiguity ------------
# The registry handles ambiguity someone wrote down. This handles the kind
# nobody has thought of yet, which is the kind that reaches a demo.

def _hit(service_id, service, score, section="الخطوات"):
    return {"service_id": service_id, "service": service, "section": section,
            "text": "نص", "source_url": "https://example.om/x", "score": score}


def _with_retrieve(monkeypatch, hits):
    monkeypatch.setattr(router, "retrieve", lambda q, k=5: hits)


# The margin check only runs on the SIMILARITY fallback, so these tests
# must use a question L1 leaves UNKNOWN. A question naming a service takes
# the whole-service path and never reaches the comparison at all.
UNRESOLVED = "كيف أحصل على وثيقة رسمية؟"


def test_two_services_within_the_margin_trigger_a_clarification(monkeypatch):
    _with_retrieve(monkeypatch, [
        _hit("mociip-licence-renewal", "تجديد الرخصة التجارية", 0.8410),
        _hit("rop-driving-licence", "تجديد رخصة القيادة", 0.8380),
    ])
    plan = plan_and_gather(UNRESOLVED)
    assert plan.strategy is Strategy.CLARIFY
    ids = {o["service_id"] for o in plan.match.clarify_options}
    assert ids == {"mociip-licence-renewal", "rop-driving-licence"}


def test_clarification_uses_the_arabic_service_names(monkeypatch):
    """The buttons must read as services, not as slugs."""
    _with_retrieve(monkeypatch, [
        _hit("mociip-licence-renewal", "تجديد الرخصة التجارية", 0.8410),
        _hit("rop-driving-licence", "تجديد رخصة القيادة", 0.8380),
    ])
    plan = plan_and_gather(UNRESOLVED)
    labels = [o["label_ar"] for o in plan.match.clarify_options]
    assert "تجديد الرخصة التجارية" in labels
    assert "تجديد رخصة القيادة" in labels
    assert "mociip" not in plan.match.clarify_question_ar


def test_a_clear_winner_does_not_trigger_a_clarification(monkeypatch):
    _with_retrieve(monkeypatch, [
        _hit("mociip-licence-renewal", "تجديد الرخصة التجارية", 0.8700),
        _hit("rop-driving-licence", "تجديد رخصة القيادة", 0.8100),
    ])
    plan = plan_and_gather(UNRESOLVED)
    assert plan.strategy is Strategy.SIMILARITY


def test_close_scores_within_the_SAME_service_are_agreement_not_ambiguity(monkeypatch):
    """
    Two sections of one service scoring alike is the system agreeing with
    itself. Comparing literal top-1 vs top-2 would misread that as a
    conflict and ask a question with two identical options.
    """
    _with_retrieve(monkeypatch, [
        _hit("rop-passport-renewal", "تجديد جواز السفر", 0.8700, "الخطوات"),
        _hit("rop-passport-renewal", "تجديد جواز السفر", 0.8695, "الرسوم"),
        _hit("rop-passport-renewal", "تجديد جواز السفر", 0.8690, "المستندات المطلوبة"),
    ])
    plan = plan_and_gather("كيف أحصل على وثيقة رسمية؟")
    assert plan.strategy is Strategy.SIMILARITY


def test_margin_is_configurable_for_d2(monkeypatch):
    """D2 tunes this alongside the threshold, so it must not be a literal."""
    import dataclasses
    monkeypatch.setattr(router, "settings",
                        dataclasses.replace(router.settings, ambiguity_margin=0.0001))
    _with_retrieve(monkeypatch, [
        _hit("mociip-licence-renewal", "تجديد الرخصة التجارية", 0.8410),
        _hit("rop-driving-licence", "تجديد رخصة القيادة", 0.8380),
    ])
    plan = plan_and_gather(UNRESOLVED)
    assert plan.strategy is Strategy.SIMILARITY   # margin now counts as decisive


def test_whole_service_path_is_unaffected_by_the_margin_check(spy):
    """
    Margin ambiguity applies ONLY to the similarity fallback. A resolved
    service has no scores to compare and must not be second-guessed.
    """
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.strategy is Strategy.WHOLE_SERVICE


def test_unknown_still_falls_through_to_retrieval(spy):
    """
    The property behind the «تجديد الملكية» anecdote, kept as a test so it
    survives the anecdote. L1 having no opinion must never become a refusal.
    """
    plan = plan_and_gather("ما الإجراءات المطلوبة قبل الفحص السنوي؟")
    assert plan.match.verdict is Verdict.UNKNOWN
    assert spy["retrieve"], "UNKNOWN must reach retrieval, not refuse"


def test_spent_clarify_budget_loads_every_candidate(spy):
    """
    With the clarifying budget gone, picking ONE candidate arbitrarily is
    right about half the time — and when it is wrong, L3 correctly reports
    it cannot answer, so the citizen gets a refusal for a question the
    other candidate would have answered.

    Loading both lets the model use whichever actually matches.
    """
    plan = plan_and_gather("أريد جواز سفر", allow_clarify=False)
    assert plan.strategy is Strategy.WHOLE_SERVICE
    assert set(spy["fetch_service"]) == {
        "rop-passport-renewal", "rop-passport-new-issuance"}
