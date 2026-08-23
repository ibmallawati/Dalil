"""
Provenance + confidence tests.

The six-field card and its «غير محدد في المصدر» padding are gone — the
answer shape now follows the question. What remains here is the part that
was never about layout, and both halves are correctness properties:

  - a citation is read off the corpus, never accepted from the model;
  - an unrecognised confidence is treated as no grounding, not ignored.
"""

import pytest

from chatbot.agent.card import (
    CONFIDENCE_VALUES,
    metadata_from,
    normalise_confidence,
    sources_from,
)


def chunk(service="تجديد جواز السفر", url="https://www.rop.gov.om/passport",
          authority="شرطة عمان السلطانية", title="تجديد جواز السفر", ref=None,
          section="الرسوم"):
    c = {
        "service": service, "authority": authority, "section": section,
        "source_url": url, "source_title": title, "text": "نص",
    }
    if ref is not None:
        c["source_ref"] = ref
    return c


# --- confidence ----------------------------------------------------------

@pytest.mark.parametrize("value", CONFIDENCE_VALUES)
def test_valid_confidence_survives(value):
    assert normalise_confidence(value) == value


@pytest.mark.parametrize("value", [None, "", "very high", "HIGH", 0.9, "maybe"])
def test_unrecognised_confidence_degrades_to_none(value):
    """
    An unreadable confidence is not evidence of grounding. Defaulting to
    "none" makes the app refuse rather than present an unverified answer.
    """
    assert normalise_confidence(value) == "none"


# --- provenance comes from the corpus ------------------------------------

def test_source_is_taken_from_the_chunk():
    meta = metadata_from([chunk()])
    assert meta["source_url"] == "https://www.rop.gov.om/passport"
    assert meta["authority"] == "شرطة عمان السلطانية"


def test_no_chunks_yields_empty_provenance_not_invented():
    meta = metadata_from([])
    assert meta["source_url"] is None
    assert meta["sources"] == []


# --- multi-source: the point of the change -------------------------------

def test_one_service_many_sections_is_cited_once():
    """
    Whole-service retrieval returns six chunks of the same file. Citing it
    six times would be noise, not rigour.
    """
    chunks = [chunk(section=s) for s in
              ("الرسوم", "الخطوات", "المستندات المطلوبة", "الفئة المستهدفة")]
    assert len(sources_from(chunks)) == 1


def test_two_services_keep_both_citations():
    """
    «كم رسوم تجديد الجواز ورخصة السياقة؟» is legitimately answered from two
    files, and the citizen is owed both citations.
    """
    chunks = [
        chunk(service="تجديد جواز السفر", url="https://www.rop.gov.om/passport"),
        chunk(service="تجديد رخصة السياقة", url="https://www.rop.gov.om/licence"),
    ]
    sources = sources_from(chunks)
    assert len(sources) == 2
    assert {s["service"] for s in sources} == {"تجديد جواز السفر", "تجديد رخصة السياقة"}


def test_sources_keep_best_match_order():
    """First-seen order = retrieval order, so the primary source leads."""
    chunks = [chunk(service="أ", url="https://a.gov.om"),
              chunk(service="ب", url="https://b.gov.om")]
    assert [s["service"] for s in sources_from(chunks)] == ["أ", "ب"]
    assert metadata_from(chunks)["service_name"] == "أ"


# --- textual attribution, for material with no live URL ------------------

def test_source_without_url_still_attributes_via_text():
    """
    A printed services guide or a PDF page has no clickable link. Honest
    attribution does not require one — it requires being present.
    """
    c = chunk(url=None, title=None, ref="دليل خدمات وزارة الداخلية، ص ١٤")
    source = sources_from([c])[0]
    assert source["url"] is None
    assert source["ref"] == "دليل خدمات وزارة الداخلية، ص ١٤"


def test_source_title_is_the_fallback_ref():
    source = sources_from([chunk(url=None, title="دليل الخدمات")])[0]
    assert source["ref"] == "دليل الخدمات"


def test_chunk_with_no_provenance_at_all_is_skipped():
    """An entry with nothing to cite must not produce an empty citation row."""
    assert sources_from([{"text": "نص", "section": "الرسوم"}]) == []
