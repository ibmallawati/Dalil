"""
D5 card schema tests — the missing-field policy.

The property under test: a field the corpus does not support renders as
«غير محدد في المصدر», never as null and never as something the model made
up. Both failure modes are silent, and one of them puts an invented
government fee underneath a real source URL.
"""

import pytest

from chatbot.agent.card import (
    CARD_FIELDS,
    NOT_IN_SOURCE,
    apply_missing_field_policy,
    empty_card,
    metadata_from,
    missing_fields,
)


def chunk(section, **kw):
    base = {
        "section": section,
        "text": f"نص {section}",
        "service": "تجديد جواز السفر",
        "service_id": "rop-passport-renewal",
        "authority": "شرطة عمان السلطانية",
        "source_url": "https://www.rop.gov.om/passport",
        "source_title": "تجديد جواز السفر",
    }
    base.update(kw)
    return base


FULL = [chunk(f.section_ar) for f in CARD_FIELDS]
# A real curated file may legitimately omit sections the official page has
# nothing for -- here, no published fee and no stated duration.
PARTIAL = [chunk(f.section_ar) for f in CARD_FIELDS
           if f.section_ar not in ("الرسوم", "المدة المتوقعة")]


# --- Missing fields are detected from the CONTEXT, before generation -----

def test_full_context_has_no_missing_fields():
    assert missing_fields(FULL) == []


def test_missing_sections_are_identified():
    assert set(missing_fields(PARTIAL)) == {"fees", "estimated_time"}


# --- The rule ------------------------------------------------------------

def test_missing_field_renders_as_not_in_source():
    card = apply_missing_field_policy({"fees": None}, PARTIAL)
    assert card["fees"] == NOT_IN_SOURCE
    assert card["estimated_time"] == NOT_IN_SOURCE


def test_missing_field_is_never_null():
    """A null field is dropped by the UI, and an absent «الرسوم» row reads
    to a citizen as 'this service is free'."""
    card = apply_missing_field_policy({}, PARTIAL)
    for f in CARD_FIELDS:
        assert card[f.key] is not None
        assert card[f.key] != ""


def test_model_invented_value_for_an_unsupported_field_is_overwritten():
    """
    THE test. The model was asked for eight fields and helpfully produced a
    fee that appears nowhere in the context. Because «الرسوم» is absent from
    the retrieved sections, the policy overwrites it structurally --
    regardless of what the model returned, and regardless of whether it
    followed the prompt.
    """
    hallucinated = {
        "fees": "٥٠ ريالاً عمانياً",
        "estimated_time": "خمسة أيام عمل",
        "steps": ["خطوة"],
    }
    card = apply_missing_field_policy(hallucinated, PARTIAL)
    assert card["fees"] == NOT_IN_SOURCE
    assert card["estimated_time"] == NOT_IN_SOURCE
    # Fields the context DOES support are left alone.
    assert card["steps"] == ["خطوة"]


def test_supported_fields_survive_untouched():
    card = apply_missing_field_policy(
        {f.key: f"قيمة {f.key}" for f in CARD_FIELDS}, FULL)
    for f in CARD_FIELDS:
        assert card[f.key] == f"قيمة {f.key}"


def test_empty_list_is_treated_as_missing():
    card = apply_missing_field_policy({"required_documents": []}, FULL)
    assert card["required_documents"] == NOT_IN_SOURCE


def test_list_fields_are_cleaned_but_kept():
    card = apply_missing_field_policy(
        {"required_documents": ["البطاقة الشخصية", "  ", ""]}, FULL)
    assert card["required_documents"] == ["البطاقة الشخصية"]


# --- Provenance comes from the corpus, not the model ---------------------

def test_source_url_is_taken_from_chunk_metadata():
    """
    A hallucinated citation is worse than no citation: it survives a glance.
    The model is never the authority on where its own context came from.
    """
    card = apply_missing_field_policy(
        {"source_url": "https://totally-made-up.example/passport"}, FULL)
    assert card["source_url"] == "https://www.rop.gov.om/passport"


def test_metadata_from_empty_context_is_none_not_invented():
    assert metadata_from([]) == {k: None for k in
                                 ("service_name", "authority", "source_url", "source_title")}


# --- Confidence (L3) -----------------------------------------------------

@pytest.mark.parametrize("value", ["high", "partial", "none"])
def test_valid_confidence_survives(value):
    assert apply_missing_field_policy({"confidence": value}, FULL)["confidence"] == value


@pytest.mark.parametrize("value", [None, "", "very high", "HIGH", 0.9, "maybe"])
def test_unparseable_confidence_degrades_to_none(value):
    """
    An unreadable confidence is not evidence of grounding. Defaulting to
    'none' makes the UI refuse rather than present an unverified card.
    """
    assert apply_missing_field_policy({"confidence": value}, FULL)["confidence"] == "none"


# --- No context at all ---------------------------------------------------

def test_empty_card_is_fully_marked_and_never_null():
    card = empty_card()
    for f in CARD_FIELDS:
        assert card[f.key] == NOT_IN_SOURCE
    assert card["confidence"] == "none"
    assert card["source_url"] is None


def test_policy_with_no_chunks_marks_everything():
    card = apply_missing_field_policy({"fees": "٢٠ ريالاً"}, [])
    assert card["fees"] == NOT_IN_SOURCE
    assert card["confidence"] == "none"


# --- The corpus contract ties the two together ---------------------------

def test_card_sections_match_the_corpus_spec_headings():
    """
    Every card field names a '##' heading the corpus is contracted to
    provide. If these drift apart, whole-service loading silently stops
    filling fields and every answer quietly degrades.
    """
    expected = {"الفئة المستهدفة", "المستندات المطلوبة", "الخطوات",
                "الرسوم", "المدة المتوقعة", "أين تتم المعاملة"}
    assert {f.section_ar for f in CARD_FIELDS} == expected
