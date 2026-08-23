"""
Router tests — L1 shapes retrieval, it no longer replaces it.

These used to assert the opposite: that a resolved service_id suppressed
similarity entirely. That bypass existed to protect a fixed answer card
top-k could not fill; the card is gone and the bypass with it, because it
was silently dropping the second half of any question spanning two
services.

The properties now under test:

  - retrieval runs for every non-refused, non-clarified question;
  - an L1-resolved service is GUARANTEED present, never exclusive;
  - the merge does not duplicate, does not reorder similarity's ranking,
    and does not fabricate scores for chunks nothing scored.

So they still assert on which retrieval function was CALLED — just for the
inverse reason.
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


# --- L1 as a hint: guaranteed present, never exclusive -------------------

def test_a_resolved_service_still_goes_through_retrieval(spy):
    """
    The inversion of the old test. L1 resolving «جواز السفر» must not stop
    the search — «كم رسوم تجديد جواز السفر ورخصة السياقة؟» resolves to the
    passport on keywords alone, and under the old bypass the driving-licence
    half of the question vanished without trace.
    """
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.strategy is Strategy.SIMILARITY
    assert spy["retrieve"] == ["كيف أجدد جواز سفري؟"]


def test_the_resolved_service_is_guaranteed_present(spy):
    """
    The other half of the bargain. Similarity can rank one section of the
    right service highly and leave the rest out, so «ما إجراءات تجديد
    الجواز؟» could arrive with the fees and not the steps. The hint pulls
    the whole file in.
    """
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert spy["fetch_service"] == ["rop-passport-renewal"]
    sections = [c["section"] for c in plan.chunks
                if c["service_id"] == "rop-passport-renewal"]
    assert len(sections) == 7


def test_similarity_ranking_leads_and_the_hint_follows(spy):
    """
    Order is the citation order — metadata_from() reads the primary source
    off chunk 0. The best-matching passage must stay first, so hinted
    sections are appended rather than prepended.
    """
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.chunks[0]["service_id"] == "x"      # what retrieval ranked top
    assert plan.chunks[1]["service_id"] == "rop-passport-renewal"


def test_hinted_sections_keep_document_order(spy):
    """Within the hinted service, sections stay as the curator wrote them."""
    plan = plan_and_gather("أريد تجديد رخصة السياقة")
    sections = [c["section"] for c in plan.chunks if c["score"] is None]
    assert sections[0] == "الفئة المستهدفة"
    assert sections.index("المستندات المطلوبة") < sections.index("الرسوم")


def test_hinted_chunks_carry_no_fabricated_score(spy):
    """
    Nothing scored them — they are present because L1 said so, not because
    similarity ranked them. A None is honest; a synthetic 1.0 would claim a
    measurement that never happened, and the margin check reads these scores.
    """
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    hinted = [c for c in plan.chunks if c["service_id"] == "rop-passport-renewal"]
    assert hinted and all(c["score"] is None for c in hinted)


def test_the_merge_does_not_duplicate_what_similarity_already_found(spy, monkeypatch):
    """
    The overlap case, which is the common one: retrieval surfaces the fee
    section of exactly the service L1 resolved. Sending it twice wastes
    context and over-weights one passage in the model's reading.
    """
    monkeypatch.setattr(router, "retrieve", lambda q, k=8: [
        {"service_id": "rop-passport-renewal", "section": "الرسوم",
         "text": "نص الرسوم", "source_url": "https://example.om/x", "score": 0.86},
    ])
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    fees = [c for c in plan.chunks if c["section"] == "الرسوم"]
    assert len(fees) == 1
    assert fees[0]["score"] == 0.86          # similarity's copy is the one kept


def test_context_size_is_capped(spy, monkeypatch):
    """
    Breadth plus depth adds up, and an unbounded context is how a long
    answer quietly becomes a truncated one — reasoning tokens bill against
    the same budget as the reply.
    """
    monkeypatch.setattr(router, "retrieve", lambda q, k=8: [
        {"service_id": f"svc-{i}", "section": "الخطوات", "text": "نص",
         "source_url": "https://example.om/x", "score": 0.9 - i / 10}
        for i in range(8)
    ])
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert len(plan.chunks) == router.MAX_CONTEXT_CHUNKS


# --- Follow-up questions: the previous service carries forward -----------
# «وكم رسومها؟» is a pronoun and a question word. Embedded alone it
# retrieves noise, and the noise trips the margin check — so the citizen was
# being asked whether they meant a home-business register, one turn after
# being told about passports.

PASSPORT_TURN = [
    {"role": "user", "content": "كيف أجدد جواز سفري؟"},
    {"role": "assistant", "content": "رسوم التجديد..."},
]


def test_a_follow_up_inherits_the_previous_service(spy):
    plan = plan_and_gather("وكم رسومها؟", history=PASSPORT_TURN)
    assert spy["fetch_service"] == ["rop-passport-renewal"]
    assert any(c["service_id"] == "rop-passport-renewal" for c in plan.chunks)


def test_a_follow_up_no_longer_asks_which_service(spy, monkeypatch):
    """
    The symptom that started this. Two unrelated services tie on noise, and
    without an inherited service the margin check reads that as genuine
    ambiguity.
    """
    monkeypatch.setattr(router, "retrieve", lambda q, k=8: [
        _hit("mociip-home-business-register", "إنشاء سجل تجاري منزلي", 0.827),
        _hit("rop-driving-licence-new", "إصدار رخصة سياقة جديدة", 0.827),
    ])
    assert plan_and_gather("وكم رسومها؟").strategy is Strategy.CLARIFY
    assert plan_and_gather("وكم رسومها؟", history=PASSPORT_TURN).strategy \
        is Strategy.SIMILARITY


def test_a_new_topic_does_not_inherit(spy):
    """
    A question that resolves on its own owns the turn. Inheriting here would
    drag the old service into an answer that never mentioned it.
    """
    plan = plan_and_gather("أريد تجديد رخصة السياقة", history=PASSPORT_TURN)
    assert spy["fetch_service"] == ["rop-driving-licence-renewal"]


def test_nothing_retrieved_still_refuses_even_with_history(spy, monkeypatch):
    """
    The guard that makes this safe, and the reason query expansion was
    rejected instead.

    «ما هي أفضل مطاعم مسقط؟» retrieves nothing and refuses for free. Pasting
    the previous question in front of it retrieves passport sections at
    0.835 — an out-of-scope question answered from the wrong service, and a
    wasted LLM call. Empty retrieval means nothing is close, so there is no
    follow-up worth serving.
    """
    monkeypatch.setattr(router, "retrieve", lambda q, k=8: [])
    plan = plan_and_gather("ما هي أفضل مطاعم مسقط؟", history=PASSPORT_TURN)
    assert plan.strategy is Strategy.REFUSE_NO_CONTENT
    assert spy["fetch_service"] == []      # the hint must not resurrect it


def test_an_uncovered_authority_still_refuses_with_history(spy):
    """L1's refusal runs before any of this and is not softened by context."""
    plan = plan_and_gather("أريد شكوى في وزارة الخارجية", history=PASSPORT_TURN)
    assert plan.strategy is Strategy.REFUSE_UNCOVERED


def test_the_most_recent_topic_wins(spy):
    """
    Read backwards. Forwards would anchor the conversation to whatever was
    asked first and never let go of it.
    """
    history = [
        {"role": "user", "content": "كيف أجدد جواز سفري؟"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "أريد تجديد رخصة السياقة"},
        {"role": "assistant", "content": "..."},
    ]
    plan_and_gather("وكم رسومها؟", history=history)
    assert spy["fetch_service"] == ["rop-driving-licence-renewal"]


def test_assistant_turns_are_not_classified(spy):
    """
    The reply names its service in prose. Classifying it would let Daleel's
    own wording, rather than the citizen's, decide the topic.
    """
    history = [{"role": "assistant",
                "content": "رسوم تجديد جواز السفر خمسة ريالات"}]
    plan_and_gather("وكم رسومها؟", history=history)
    assert spy["fetch_service"] == []


def test_no_history_behaves_exactly_as_before(spy):
    plan_and_gather("وكم رسومها؟")
    plan_and_gather("وكم رسومها؟", history=[])
    assert spy["fetch_service"] == []


# --- CLARIFY: deterministic, no LLM, no embedding ------------------------

def test_ambiguous_passport_question_asks_instead_of_guessing(spy):
    plan = plan_and_gather("أريد جواز سفر")
    assert plan.strategy is Strategy.CLARIFY
    assert spy["fetch_service"] == [] and spy["retrieve"] == []
    assert plan.match.clarify_question_ar
    assert len(plan.match.clarify_options) == 2
    ids = {o["service_id"] for o in plan.match.clarify_options}
    assert ids == {"rop-passport-renewal", "rop-passport-issuance"}


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
    assert plan.strategy is not Strategy.CLARIFY
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
    assert plan.strategy is not Strategy.CLARIFY
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
    plan = plan_and_gather("أريد شكوى في وزارة الخارجية")
    assert plan.strategy is Strategy.REFUSE_UNCOVERED
    assert plan.is_refusal
    assert spy["fetch_service"] == [] and spy["retrieve"] == []


def test_registry_covers_a_service_the_corpus_lacks(spy, monkeypatch, caplog):
    """
    The registry and the corpus can disagree — the registry declares a
    service no .md file provides. Under the old bypass that was an immediate
    refusal, because the empty fetch was the only thing tried.

    Retrieval is now consulted anyway, so a near neighbour can still answer.
    The disagreement is a data-quality bug either way, so it is logged.
    """
    monkeypatch.setattr(router, "fetch_service", lambda service_id: [])
    with caplog.at_level("INFO"):
        plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.grounded
    assert plan.strategy is Strategy.SIMILARITY
    assert "route_service_has_no_content" in caplog.text


def test_nothing_anywhere_is_still_a_refusal(spy, monkeypatch):
    """Empty registry file AND empty retrieval — D6 path 1."""
    monkeypatch.setattr(router, "fetch_service", lambda service_id: [])
    monkeypatch.setattr(router, "retrieve", lambda q, k=8: [])
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
    plan = plan_and_gather("أريد تجديد جواز سفري ثم مراجعة وزارة الخارجية")
    assert plan.match.verdict is Verdict.COVERED
    assert not plan.is_refusal
    assert spy["fetch_service"] == ["rop-passport-renewal"]


def test_the_uncovered_authority_is_still_recorded_for_a_footnote(spy):
    """
    Covered wins, but the citizen did ask about the other authority. The
    answer should be able to say so rather than silently ignore half the
    question.
    """
    plan = plan_and_gather("أريد تجديد جواز سفري ثم مراجعة وزارة الخارجية")
    assert plan.match.also_mentioned_uncovered_ar == "وزارة الخارجية"


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


def test_hinted_chunks_cannot_trigger_the_margin_check(spy):
    """
    The hint now travels alongside scored results, so the margin check sees
    a second service in the list where it never used to. Nothing scored the
    hinted chunks, and carrying score=None rather than a synthetic number is
    what keeps them out of the rival comparison.
    """
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.strategy is Strategy.SIMILARITY
    assert {c["service_id"] for c in plan.chunks} == {"x", "rop-passport-renewal"}


def test_l1_resolution_outranks_a_similarity_tie_break(spy, monkeypatch):
    """
    The boundary of the demotion, and a live regression when it first
    landed. On the real corpus «كيف أجدد جواز سفري؟» retrieved
    mht-tour-guide-licence-renewal at 0.8636 against rop-passport-renewal
    at 0.8611 — so Daleel asked the citizen whether they meant a TOUR GUIDE
    LICENCE, for a question that says «جواز سفري» in plain Arabic.

    e5 scores «تجديد» heavily and 0.0025 between those two is noise. The
    margin check is the net for ambiguity nobody DECLARED; once L1 has
    resolved a service, the tie is already broken and asking is a
    regression, not caution.
    """
    monkeypatch.setattr(router, "retrieve", lambda q, k=8: [
        _hit("mht-tour-guide-licence-renewal", "تجديد ترخيص مرشد سياحي", 0.8636),
        _hit("rop-passport-renewal", "تجديد جواز السفر", 0.8611),
    ])
    plan = plan_and_gather("كيف أجدد جواز سفري؟")
    assert plan.match.service_id == "rop-passport-renewal"
    assert plan.strategy is Strategy.SIMILARITY


def test_the_margin_check_still_fires_when_l1_has_no_opinion(spy, monkeypatch):
    """The suppression above is scoped to a resolved service, not global."""
    monkeypatch.setattr(router, "retrieve", lambda q, k=8: [
        _hit("mht-tour-guide-licence-renewal", "تجديد ترخيص مرشد سياحي", 0.8636),
        _hit("rop-passport-renewal", "تجديد جواز السفر", 0.8611),
    ])
    plan = plan_and_gather("كيف أحصل على وثيقة رسمية؟")
    assert plan.match.service_id is None
    assert plan.strategy is Strategy.CLARIFY


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
        "rop-passport-renewal", "rop-passport-issuance"}
