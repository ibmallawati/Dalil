"""
D6 Layer 1 tests — the deterministic scope pre-gate.

The behaviour under test is not "does keyword matching work" but the three
verdicts and, above all, that UNKNOWN does NOT refuse. A gate that refuses
whatever it fails to recognise would reject valid questions phrased in
words nobody thought to list, and it would do so invisibly.
"""

import pytest

from chatbot.agent.registry import (
    Verdict,
    classify,
    coverage_summary,
    uncovered_authority_message,
    unknown_service_message,
)


# --- COVERED: proceed to retrieval ---------------------------------------

@pytest.mark.parametrize("question,expected_service", [
    ("كيف أجدد جواز سفري؟", "rop-passport-renewal"),
    ("ما هي رسوم تجديد الجواز؟", "rop-passport-renewal"),
    ("أريد تجديد البطاقة الشخصية", "rop-id-renewal"),
    ("كيف اجدد ملكية سيارتي؟", "rop-vehicle-registration-renewal"),
    ("أريد تجديد رخصة السياقة", "rop-driving-licence-renewal"),
    ("أريد إصدار رخصة سياقة جديدة", "rop-driving-licence-new"),
    ("كيف أسجل سجل تجاري جديد؟", "mociip-commercial-registration"),
    ("أريد تجديد الرخصة التجارية القائمة", "mociip-licence-renewal"),
])
def test_covered_services_are_recognised(question, expected_service):
    match = classify(question)
    assert match.verdict is Verdict.COVERED
    assert match.service_id == expected_service
    assert match.should_retrieve
    assert not match.needs_clarification


def test_authority_named_without_a_service_is_still_covered():
    """«ما هي خدمات شرطة عمان؟» is in scope even with no service named."""
    match = classify("ما هي خدمات شرطة عمان السلطانية؟")
    assert match.verdict is Verdict.COVERED
    assert match.authority_id == "rop"
    assert match.service_id is None


def test_service_recognised_without_naming_the_authority():
    """
    Most citizens never name the authority. If service matching did not
    carry this, almost every real question would land in UNKNOWN.
    """
    match = classify("متى ينتهي جوازي؟")
    assert match.verdict is Verdict.COVERED
    assert match.authority_id == "rop"


def test_d3_normalization_applies_to_matching():
    """
    «الهويه» vs «الهوية» — D3 folds these, so the registry file does not
    have to enumerate spellings it already handles. Both sides normalize.
    """
    assert classify("تجديد الهوية الشخصية").verdict is Verdict.COVERED
    assert classify("تجديد الهويه الشخصيه").verdict is Verdict.COVERED


def test_longest_variant_wins():
    """«الرخصة التجارية» must beat the shorter «رخصة» of driving licence."""
    match = classify("أريد تجديد الرخصة التجارية لمحلي")
    assert match.service_id == "mociip-licence-renewal"


# --- UNCOVERED_AUTHORITY: the only verdict that refuses without retrieving

@pytest.mark.parametrize("question,expected_ar", [
    ("ما هي إجراءات التسجيل في وزارة التربية والتعليم؟", "وزارة التربية والتعليم"),
    ("أريد الاستفسار عن وزارة العمل", "وزارة العمل"),
    ("كيف أتواصل مع بلدية مسقط؟", "بلدية مسقط"),
])
def test_known_but_uncovered_authorities_refuse_immediately(question, expected_ar):
    match = classify(question)
    assert match.verdict is Verdict.UNCOVERED_AUTHORITY
    assert match.authority_ar == expected_ar
    assert not match.should_retrieve


def test_uncovered_message_names_the_authority_and_states_coverage():
    match = classify("وزارة العمل")
    msg = uncovered_authority_message(match)
    assert "وزارة العمل" in msg
    assert "قريبًا" in msg
    # The refusal must also say what Daleel CAN do, or it is a dead end.
    assert "شرطة عمان السلطانية" in msg


# --- UNKNOWN: falls through, does NOT refuse -----------------------------

@pytest.mark.parametrize("question", [
    "كيف أحصل على وثيقة رسمية؟",
    "ما هي الإجراءات المطلوبة؟",
    "أحتاج مساعدة",
    "ما هي أفضل مطاعم مسقط؟",
])
def test_unrecognised_questions_fall_through_rather_than_refusing(question):
    """
    The critical property of this layer. Keyword matching has no opinion
    about these, and 'no opinion' must never become 'no'. Layers 2 and 3
    judge them.
    """
    match = classify(question)
    assert match.verdict is Verdict.UNKNOWN
    assert match.should_retrieve


def test_empty_question_is_unknown_not_a_crash():
    for value in ("", "   ", None):
        assert classify(value).verdict is Verdict.UNKNOWN


# --- D6's two refusal paths are genuinely different ----------------------

def test_the_two_refusals_say_different_things():
    """
    "I don't have this procedure" and "that authority isn't covered yet"
    are different statements to a citizen, and D6 exists to keep them
    distinct rather than collapsing both into a generic apology.
    """
    covered = classify("كيف أجدد جواز سفري؟")
    uncovered = classify("وزارة العمل")

    path_1 = unknown_service_message(covered)
    path_2 = uncovered_authority_message(uncovered)

    assert path_1 != path_2
    assert "https://" in path_1          # path 1 hands over the official portal
    assert "قريبًا" in path_2            # path 2 promises future coverage


# --- The claim made to the panel -----------------------------------------

def test_coverage_summary_reports_scope():
    summary = coverage_summary()
    assert summary["authority_count"] == 4
    # 17 after promoting MoH and MoHUP from not_covered on 2 Aug:
    # 8 ROP + 4 MoCIIP + 2 MoH + 3 MoHUP.
    assert summary["service_count"] == 17
    assert summary["known_uncovered_count"] > 0
    ids = {a["id"] for a in summary["authorities"]}
    assert ids == {"rop", "mociip", "moh", "mohup"}


def test_registry_service_ids_match_the_corpus_spec_naming():
    """
    Every service_id in the registry must be a filename the corpus can
    actually provide. A mismatch means the gate admits a question that
    retrieval can never answer.
    """
    summary = coverage_summary()
    for authority in summary["authorities"]:
        for service_id in authority["services"]:
            assert service_id.startswith(authority["id"] + "-"), service_id


# --- The MoCIIP collision that D4 will need ------------------------------

def test_question_naming_two_services_surfaces_both_candidates():
    """
    A question naming more than one covered service reports ALL of them.
    That list is what the margin check and D4 consume; collapsing it to
    top-1 here would hide the ambiguity from every layer above.
    """
    match = classify("أريد تجديد السجل التجاري والرخصة التجارية")
    assert match.verdict is Verdict.COVERED
    assert len(match.candidate_service_ids) >= 2
    assert "mociip-commercial-register-renewal" in match.candidate_service_ids
    assert "mociip-licence-renewal" in match.candidate_service_ids


# --- Disambiguation is a RESOLUTION, not a non-event ---------------------

def test_disambiguated_phrase_resolves_to_its_service():
    """
    Regression. An earlier version returned "not ambiguous" when the
    disambiguators settled it, and left ordinary variant matching to pick
    the service up afterwards. That worked only while the phrase was BOTH
    an ambiguity trigger AND a service variant.

    On the first real corpus file it was only a trigger, so
    «كيف أجدد جواز سفري؟» disambiguated to renewal and then fell all the
    way through to the similarity fallback — losing sections and reporting
    «غير محدد في المصدر» for fees the corpus actually had.
    """
    match = classify("كيف أجدد جواز سفري؟")
    assert match.verdict is Verdict.COVERED
    assert match.service_id == "rop-passport-renewal"
    assert not match.needs_clarification


def test_new_issuance_wording_resolves_the_other_way():
    match = classify("أريد إصدار جواز سفر جديد")
    assert match.service_id == "rop-passport-new-issuance"
    assert not match.needs_clarification


def test_bare_passport_request_is_genuinely_ambiguous():
    """
    «أريد جواز سفر» does not say new or renewal, and the two procedures
    differ: an under-18 new issuance must be submitted by the guardian,
    a renewal by the applicant themselves.
    """
    match = classify("أريد جواز سفر")
    assert match.needs_clarification
    assert set(match.candidate_service_ids) == {
        "rop-passport-renewal", "rop-passport-new-issuance"}


# --- Disambiguator matching: neither too loose nor too strict ------------

@pytest.mark.parametrize("question", [
    "ما المستندات المطلوبة لتجديد الجواز؟",     # ل proclitic
    "وين أستلم الجواز بعد التجديد؟",            # ال proclitic
    "جوازي منتهي، شو أسوي؟",                    # Arabic comma glued on
    "كم أدفع لتجديد جواز سفر طفلي؟",            # ل proclitic
    "أريد تجديد جواز سفري",                     # bare
])
def test_clear_renewal_wording_never_asks_to_clarify(question):
    """
    The too-strict half. Arabic glues proclitics (ل، ال، ب، و) onto words
    and punctuation onto their tails, so plain space-delimited matching
    fails on wording that is completely unambiguous to a reader.

    Measured before this fix: five of eight plainly-worded passport
    questions were being asked to clarify. An agent that interrogates
    someone who already answered reads as broken.
    """
    match = classify(question)
    assert not match.needs_clarification, question
    assert match.service_id == "rop-passport-renewal"


def test_new_issuance_wording_still_resolves_the_other_way():
    for question in ["أريد إصدار جواز سفر جديد", "استخراج جواز لمولود جديد"]:
        assert classify(question).service_id == "rop-passport-new-issuance"


def test_proclitic_tolerance_does_not_reintroduce_the_substring_bug():
    """
    THE constraint. «ت» is not a proclitic, so «جديد» must still fail to
    match inside «تجديد» — otherwise "renew" resolves to "new", exactly
    backwards, which is the bug word-boundary matching was added to stop.
    """
    from chatbot.agent.registry import _has_word
    assert not _has_word("اريد تجديد جواز سفري", "جديد")
    assert _has_word("اريد تجديد جواز سفري", "تجديد")
    assert _has_word("لتجديد الجواز", "تجديد")
    assert _has_word("جوازي منتهي، شو اسوي", "منتهي")
