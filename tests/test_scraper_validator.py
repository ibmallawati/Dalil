"""
Extraction validator tests.

This is the component standing between a language model and the corpus.
An invented fee that gets past it becomes a permanent, embedded,
officially-cited lie, so the tests here are mostly about what it must
REJECT — and, just as importantly, what it must not reject, because a
validator that cries wolf gets switched off.
"""

import pytest

from chatbot.scraper.validator import summarise, validate

RAW = """خدمة تجديد جواز السفر
الشروط الواجبة
أن تكون الرخصة صادرة من شرطة عمان السلطانية
المستندات المطلوبة
أصل البطاقة الشخصية سارية المفعول
جواز السفر العماني السابق
الرسوم
من هم في سن 18 فما دون: 5
لمن تجاوز سن 18: 10
يضاف مبلغ وقدره 5
"""


def body(*sections):
    return "\n\n".join(f"## {h}\n\n{t}" for h, t in sections)


# --- Must REJECT ---------------------------------------------------------

def test_invented_fee_is_caught():
    """
    THE test. The page says 5 and 10; the model wrote 50. Nothing else in
    the pipeline would notice.
    """
    md = body(("الرسوم", "الرسوم المقررة هي 50 ريال عماني."))
    findings = validate(md, RAW, "تجديد جواز السفر")
    assert any(f.check == "number_not_in_source" for f in findings)
    assert "50" in str(findings[0])


def test_invented_document_is_caught():
    md = body(("المستندات المطلوبة", "- شهادة الميلاد\n- أصل البطاقة الشخصية سارية المفعول"))
    findings = validate(md, RAW, "تجديد جواز السفر")
    assert any(f.check == "document_not_in_source" for f in findings)
    assert "شهادة الميلاد" in str(findings[0])


def test_empty_section_is_caught():
    md = body(("الرسوم", "5"), ("المدة المتوقعة", ""))
    findings = validate(md, RAW, "تجديد جواز السفر")
    assert any(f.check == "section_too_short" for f in findings)


def test_service_name_not_on_the_page_is_caught():
    findings = validate(body(("الرسوم", "5 و 10")), RAW, "تجديد رخصة القيادة")
    assert any(f.check == "service_name_not_in_source" for f in findings)


def test_file_with_no_sections_is_caught():
    assert any(f.check == "no_sections" for f in validate("", RAW, "تجديد جواز السفر"))


# --- Must ACCEPT: the real-page quirks that must not trip it -------------

def test_fees_with_no_currency_unit_pass():
    """
    The ROP pages state «5» and «10» with no unit. Carrying them through
    unitless is CORRECT behaviour, and must not read as a defect.
    """
    assert validate(body(("الرسوم", "من هم في سن 18 فما دون: 5\nلمن تجاوز سن 18: 10")),
                    RAW, "تجديد جواز السفر") == []


def test_ambiguous_unexplained_amount_passes_through():
    """
    «يضاف مبلغ وقدره 5» — the page never says what the 5 is for. It must be
    carried verbatim, not interpreted, and not rejected.
    """
    assert validate(body(("الرسوم", "يضاف مبلغ وقدره 5")),
                    RAW, "تجديد جواز السفر") == []


def test_ordered_list_numbering_is_not_treated_as_content():
    """
    The "1." in "1. تقديم الطلب" is formatting the structurer added. Counting
    it as a figure would fail every file that has steps.
    """
    md = body(("الخطوات", "1. تقديم الطلب\n2. دفع الرسوم\n3. استلام الجواز"))
    assert [f for f in validate(md, RAW, "تجديد جواز السفر")
            if f.check == "number_not_in_source"] == []


def test_arabic_indic_digits_match_ascii_digits():
    """
    A page written «٥» and a file written «5» are the same number. D3
    normalization folds them, so this must not be a false failure.
    """
    raw_ar = "الرسوم\nمن هم في سن ١٨ فما دون: ٥\nخدمة تجديد جواز السفر"
    assert validate(body(("الرسوم", "5 لمن هم دون 18")),
                    raw_ar, "تجديد جواز السفر") == []


def test_markdown_fee_table_passes():
    """A table's separator row is syntax, and its figures are content."""
    table = ("| الفئة | قيمة الرسوم (بـ ) |\n|---|---|\n"
             "| دون 18 | 5 |\n| فوق 18 | 10 |")
    assert validate(body(("الرسوم", table)), RAW, "تجديد جواز السفر") == []


def test_reformatted_document_line_still_matches():
    """
    The model reformats punctuation and bullets. «- أصل البطاقة الشخصية
    سارية المفعول.» against a page line without the dash or full stop is a
    formatting difference, not a fabrication.
    """
    md = body(("المستندات المطلوبة", "- أصل البطاقة الشخصية سارية المفعول."))
    assert [f for f in validate(md, RAW, "تجديد جواز السفر")
            if f.check == "document_not_in_source"] == []


def test_a_clean_file_produces_no_findings():
    md = body(
        ("المستندات المطلوبة", "- أصل البطاقة الشخصية سارية المفعول\n- جواز السفر العماني السابق"),
        ("الرسوم", "من هم في سن 18 فما دون: 5\nلمن تجاوز سن 18: 10"),
    )
    assert validate(md, RAW, "تجديد جواز السفر") == []


# --- The failure note has to be actionable -------------------------------

def test_summary_names_the_offending_value():
    md = body(("الرسوم", "الرسوم 50 ريال"))
    text = summarise(validate(md, RAW, "تجديد جواز السفر"))
    assert "number_not_in_source" in text
    assert "50" in text


def test_summary_of_a_pass_says_so():
    assert "PASSED" in summarise([])
