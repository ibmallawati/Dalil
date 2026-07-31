"""
D3 Arabic normalization tests.

These live in the repo (not the scratchpad) on purpose: normalize_arabic()
is the one function that MUST behave identically on the index path and the
query path, and a regression in it degrades retrieval silently -- no error,
no exception, just worse answers. That is exactly the class of bug a test
suite has to hold down.
"""

import pytest

from chatbot.rag.arabic import normalize_arabic


# --- Structural normalization (always on) --------------------------------

def test_strips_tashkeel():
    # Official pages occasionally carry diacritics; citizens never type them.
    assert normalize_arabic("جَوَازُ سَفَرٍ") == normalize_arabic("جواز سفر")


def test_removes_tatweel():
    assert normalize_arabic("جـــواز") == "جواز"


def test_arabic_indic_digits_become_ascii():
    assert normalize_arabic("٥٠٠ ريال") == "500 ريال"
    # Extended/Persian forms too.
    assert normalize_arabic("۵۰۰") == "500"


def test_collapses_whitespace():
    assert normalize_arabic("  تجديد   جواز \n السفر  ") == "تجديد جواز السفر"


def test_strips_invisible_joiners_and_bom():
    assert normalize_arabic("﻿جواز‌سفر") == "جوازسفر"


def test_nfkc_folds_presentation_forms():
    """
    The D3 pre-flight gate found real Arabic PDFs extracting as glyph
    presentation forms (U+FB50-U+FEFF) rather than letters. Curated markdown
    should never contain these, but the OCR/PDF route can still emit them,
    so NFKC is the safety net that keeps that route usable.
    """
    presentation = "ﻚﺘﺮ"          # glyph forms
    assert not any(0xFB50 <= ord(c) <= 0xFEFF for c in normalize_arabic(presentation))


def test_empty_and_none_safe():
    assert normalize_arabic("") == ""
    assert normalize_arabic(None) == ""


# --- Orthographic folding (toggleable) -----------------------------------

@pytest.mark.parametrize("written,typed", [
    ("أحمد", "احمد"),      # hamza above
    ("إقامة", "اقامة"),     # hamza below
    ("آسيا", "اسيا"),       # madda
])
def test_alef_variants_fold_together(written, typed):
    """Omani users routinely type a bare alef. Both spellings must match."""
    assert normalize_arabic(written) == normalize_arabic(typed)


def test_teh_marbuta_and_alef_maqsura_fold():
    assert normalize_arabic("هوية") == normalize_arabic("هويه")
    assert normalize_arabic("مستشفى") == normalize_arabic("مستشفي")


def test_orthographic_folding_can_be_disabled():
    """
    The flag exists because folding is a genuine trade-off for a DENSE
    encoder: it absorbs typing variance but hands the tokenizer spellings
    it never saw in training. D7 decides empirically; the flag is what
    makes that experiment possible.
    """
    assert normalize_arabic("هوية", fold_orthography=False) == "هوية"
    assert normalize_arabic("هوية", fold_orthography=True) == "هويه"


# --- The property that actually matters ----------------------------------

def test_idempotent():
    """
    Normalizing twice must equal normalizing once. build_index.py and
    retrieval.py both call this, and a non-idempotent transform would make
    the two paths disagree the moment one of them normalized upstream.
    """
    q = "كيف أجدد جواز سفري؟ الرسوم ٢٠ ريالاً"
    once = normalize_arabic(q)
    assert normalize_arabic(once) == once


def test_index_and_query_paths_agree():
    """
    The core D3 guarantee, stated as a test: the same question written the
    way a citizen types it and the way a document spells it must produce
    identical text to embed.
    """
    document_spelling = "تجديد جَواز السَفر لِلمواطنين"
    citizen_spelling = "تجديد جواز السفر للمواطنين"
    assert normalize_arabic(document_spelling) == normalize_arabic(citizen_spelling)
