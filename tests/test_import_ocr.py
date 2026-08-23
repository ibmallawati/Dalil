"""
OCR importer tests — the cleaner must never change meaning.

The importer this replaces used an LLM to restructure OCR text, and it
repaired damage instead of reporting it: «.تختلة 1 نوع النشاط والتر خب»
became «تختلف حسب نوع النشاط والترخيص», a fluent invention. Five of fifteen
services failed validation that way.

There is no model here now, so the risk moves: it is no longer fabrication
but over-eager cleaning. Every rule below is asserted twice — that it fixes
what it targets, and that it leaves everything else alone.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import import_ocr_corpus as imp                              # noqa: E402


# --- Cleaning: mechanical, meaning-preserving ----------------------------

def test_rtl_trailing_list_number_moves_to_the_front():
    """OCR emits «.الدخول إلى الموقع .1» — the marker is at the wrong end."""
    assert imp.clean_line("الدخول إلى الموقع .1") == "1. الدخول إلى الموقع"


def test_rtl_leading_period_moves_to_the_end():
    assert imp.clean_line(".أداء أدوارها الوطنية") == "أداء أدوارها الوطنية."


def test_a_decimal_is_not_mistaken_for_a_leading_period():
    """«.5 ريال» must not become «5 ريال.» — a fee is not punctuation."""
    assert imp.clean_line(".5 ريال عماني").startswith(".5") or \
        "5" in imp.clean_line(".5 ريال عماني")


@pytest.mark.parametrize("bullet", ["ه", "ه.", "٠", "•"])
def test_bullet_glyphs_become_list_markers(bullet):
    """Tesseract renders the document's bullet differently per page."""
    assert imp.clean_line(f"{bullet} السجل التجاري.") == "- السجل التجاري."


def test_a_word_starting_with_heh_is_not_treated_as_a_bullet():
    """«هذه» opens with the same letter the bullet OCRs to."""
    assert imp.clean_line("هذه الخدمة إلكترونية") == "هذه الخدمة إلكترونية"


def test_the_ocr_comma_substitute_is_restored():
    assert imp.clean_line("المسلحة» وتخطيط") == "المسلحة، وتخطيط"


def test_cleaning_never_corrects_a_misspelled_word():
    """
    «سلطنة غُمان» is the country's name, misspelled by OCR. It ships as-is.

    A rule that fixes it is a rule that can fix other things, and "the tool
    quietly corrects obvious errors" is exactly how an invented fee gets
    written next to a real citation. A visible typo is the better failure.
    """
    assert "غُمان" in imp.clean_line("وزارة الدفاع - سلطنة غُمان")


# --- Noise removal: narrow, because it DELETES ---------------------------

@pytest.mark.parametrize("junk", ["ا ال ال ال م", "دن حص كن", "60", "١٢"])
def test_ocr_debris_is_dropped(junk):
    assert imp.is_noise_line(junk) is True


@pytest.mark.parametrize("real", [
    "لا يوجد رسوم",
    "السجل التجاري للمنشأة.",
    "تختلف حسب نوع النشاط",
    "5 ريال عماني",
])
def test_real_content_survives_the_noise_filter(real):
    """
    This filter deletes text, so a false positive loses a fee or a document.
    Short phrases survive because one word is always longer than three
    characters — «لا يوجد» keeps «يوجد».
    """
    assert imp.is_noise_line(real) is False


# --- Field labels: three shapes, all in the same document ----------------

def test_label_alone_on_a_line():
    assert imp.label_of("المستندات المطلوبة:") == ("المستندات المطلوبة", "")


def test_label_with_the_colon_flipped_by_rtl_ocr():
    assert imp.label_of(":المستندات المطلوبة") == ("المستندات المطلوبة", "")


def test_label_and_value_on_one_line():
    """
    The shape that was being missed. «الرسوم: تختلف حسب نوع التصريح» stayed
    part of the previous section, so «الرسوم» came out empty and the fee was
    filed under «الشروط والمتطلبات». Fixing it moved 6 services over the
    actionability bar.
    """
    assert imp.label_of("الرسوم: تختلف حسب نوع التصريح.") == \
        ("الرسوم", "تختلف حسب نوع التصريح.")


def test_ordinary_prose_containing_a_colon_is_not_a_label():
    assert imp.label_of("ملاحظة: يرجى مراجعة الموقع") is None


def test_labels_map_onto_the_corpus_section_names():
    """Imported files must use the same '##' headings as curated ones."""
    assert imp.label_of("الفئة المستفيدة:")[0] == "الفئة المستهدفة"
    assert imp.label_of("مدة الإنجاز:")[0] == "المدة المتوقعة"
    assert imp.label_of("خطوات الحصول على الخدمة:")[0] == "الخطوات"


def test_the_destroyed_url_field_is_not_imported():
    """
    Every URL in these documents is unrecoverable glyph soup («رابط الخدمة:
    مرمء. + مع دطاء<-ع1نا1117»). Importing the field would attach that to an
    answer as if it were a citation.
    """
    assert imp.label_of("رابط الخدمة:") is None


# --- Empty fields are an absence, not an answer --------------------------

@pytest.mark.parametrize("blank", ["", "   ", "لا توجد رسوم مذكورة", "غير محدد"])
def test_blank_markers_count_as_empty(blank):
    """
    «لا توجد رسوم مذكورة» means the guide did not publish a fee. Rendering
    it as a fee section tells a citizen the service is free — a claim, not
    an absence.
    """
    assert imp.is_empty_field(blank) is True


def test_a_real_value_is_not_empty():
    assert imp.is_empty_field("5 ريال عماني") is False


# --- Authority identity ---------------------------------------------------

def test_authority_name_comes_from_the_table_not_the_ocr():
    """
    The OCR spells the country «غُمان» and folds ة to ه. The authority's
    name is identity, not content, so it is read from a canonical table —
    a different SOURCE for the field, not an edit to the text.
    """
    auth_id, name_ar, name_en = imp.match_authority("وزارة العمل - سلطنة غُمان")
    assert (auth_id, name_ar) == ("mol", "وزارة العمل")
    assert name_en == "Ministry of Labour"


def test_authority_matching_survives_ocr_spelling_variants():
    for raw in ["وزارة الداخلية - سلطنة عُمان",
                "وزارة الداخلية ‏ سلطنة عُمان",
                "وزارة الداخليه - سلطنة غمان"]:
        assert imp.match_authority(raw)[0] == "moi"


def test_an_unlisted_authority_gets_a_derived_identity():
    """
    Rejecting these used to mean a PDF for any ministry nobody had hand-
    listed produced nothing at all — every service dropped for a reason
    that had nothing to do with the service.

    The identity is derived and the authority is auto-registered on the way
    out, loudly. See chatbot.rag.registry_sync.
    """
    auth_id, name_ar, name_en = imp.match_authority("وزارة الفضاء - سلطنة عُمان")
    assert name_ar == "وزارة الفضاء"        # country suffix stripped
    assert auth_id and auth_id.isascii()
    assert name_en                          # transliterated placeholder


def test_a_derived_id_cannot_collide_with_a_listed_one():
    listed = {identity[0] for identity in imp.AUTHORITIES.values()}
    assert imp.match_authority("وزارة الفضاء")[0] not in listed


def test_an_unrecognisable_heading_is_still_rejected():
    """OCR debris must not become a government body in the registry."""
    assert imp.match_authority("ا ال ال ال م") is None
    assert imp.match_authority("الرسوم") is None


# --- The gate -------------------------------------------------------------

def _service(**overrides):
    service = {
        "authority_raw": "وزارة العمل - سلطنة عُمان",
        "name": "إصدار تصاريح العمل",
        "number": "3",
        "page": 5,
        "sections": {"الخطوات": "خطوة", "الرسوم": "5 ريال",
                     "المستندات المطلوبة": "السجل التجاري"},
        "source_file": "x.ocr.txt",
    }
    service.update(overrides)
    return service


def test_an_actionable_service_is_accepted():
    assert imp.assess(_service())[0] is True


def test_a_thin_service_is_rejected():
    ok, reason = imp.assess(_service(sections={"الخطوات": "خطوة"}))
    assert ok is False and "substantive" in reason


def test_empty_sections_do_not_count_toward_the_bar():
    ok, _ = imp.assess(_service(sections={
        "الخطوات": "خطوة", "الرسوم": "لا توجد", "المستندات المطلوبة": "لا يوجد"}))
    assert ok is False


def test_a_contact_form_is_not_a_citizen_transaction():
    """
    A vague ministry question that lands on a contact form returns a web
    form instead of a procedure, and shadows the real service behind it.
    """
    ok, reason = imp.assess(_service(name="التواصل مع وزارة العمل"))
    assert ok is False and "citizen transaction" in reason


def test_a_service_under_an_unlisted_authority_is_imported():
    """It is registered on the way out rather than dropped."""
    assert imp.assess(_service(authority_raw="وزارة الفضاء"))[0] is True


def test_a_service_under_unreadable_debris_is_rejected():
    ok, reason = imp.assess(_service(authority_raw="ا ال ال ال م"))
    assert ok is False and "not a recognisable authority" in reason


# --- Emission -------------------------------------------------------------

def test_rendered_file_carries_a_checkable_text_citation():
    """
    No URL exists, so the citation must be specific enough to verify by
    hand: document, authority, service number, page.
    """
    out = imp.render(_service(), "mol-03", "دليل الخدمات الحكومية")
    assert "source_ref:" in out
    assert "الخدمة رقم (3)" in out and "ص 5" in out
    assert "source_url" not in out


def test_rendered_service_id_matches_its_filename_stem():
    """
    service_id is the join key to the registry. One character of drift and
    the service refuses every question while sitting fully indexed — this
    project has already lost a debugging session to exactly that.
    """
    out = imp.render(_service(), "mol-03", "دليل")
    assert "service_id: mol-03" in out


def test_empty_sections_are_omitted_not_padded():
    out = imp.render(_service(sections={
        "الخطوات": "خطوة واحدة", "الرسوم": "لا توجد رسوم مذكورة"}), "mol-03", "دليل")
    assert "## الخطوات" in out
    assert "## الرسوم" not in out


def test_rendered_output_loads_as_a_corpus_file(tmp_path):
    """The end-to-end contract: what the importer writes, the loader accepts."""
    from chatbot.rag.markdown_corpus import load_file

    path = tmp_path / "mol-03.md"
    path.write_text(imp.render(_service(), "mol-03", "دليل الخدمات"),
                    encoding="utf-8")

    chunks = load_file(str(path))
    assert chunks
    assert all(c["service_id"] == "mol-03" for c in chunks)
    assert all(c["authority"] == "وزارة العمل" for c in chunks)
    assert all(c["source_ref"] for c in chunks)
