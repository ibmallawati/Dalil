"""
arabic.py — D3 Arabic text normalization.

ONE function, called from BOTH sides of retrieval:
  - index time  (scripts/build_index.py, before embedding a passage)
  - query time  (chatbot.rag.retrieval, before embedding a question)

That "one function, two callers" shape is the whole point. If the two paths
normalize differently — even slightly — every query is compared against
vectors built from subtly different text, and retrieval degrades with no
error, no warning, and no failing test. Never inline a variant of this.

--- What gets normalized, and why it is split in two ---------------------

STRUCTURAL (always on). Removes encoding noise that carries no meaning:
  - NFKC: folds Arabic presentation forms (U+FB50-U+FEFF) back to normal
    letters. Curated markdown should never contain these, but the PDF/OCR
    ingestion route can still emit them (see the D3 pre-flight findings),
    so this is the safety net that keeps that route usable.
  - tashkeel (diacritics) and tatweel: decorative; a citizen typing a
    question will never include them.
  - Arabic-Indic digits -> ASCII: '٥٠٠ ريال' and '500 ريال' must match.
  - whitespace collapse.

ORTHOGRAPHIC (toggleable, on by default). Folds letters that Omani users
routinely type interchangeably:
  - أ إ آ ٱ -> ا      (people type the bare alef)
  - ة -> ه            (people type either)
  - ى -> ي            (people type either)

The split exists because this stack now uses a DENSE multilingual encoder
(e5), not lexical matching. Orthographic folding is unambiguously correct
for keyword search, but for a neural model it is a real trade-off: it
absorbs user typing variance, and it also feeds the tokenizer spellings it
never saw in training. Which effect wins is an empirical question about
Arabic specifically, not something to assert.

So it is a flag, and D7 answers it: run the evaluation set with it on and
off and keep whichever scores better. Both sides read the same default, so
the two paths cannot drift apart while that experiment runs.
"""

import re
import unicodedata

# Tashkeel: fathatan..sukun, plus superscript alef and the Quranic marks
# that occasionally survive a copy-paste from an official page.
_TASHKEEL = re.compile(r"[ً-ْٰۖ-ۭ]")

# Tatweel / kashida — pure typographic stretching, zero semantic content.
_TATWEEL = "ـ"

# Zero-width joiners and the BOM: invisible, and they split tokens.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")

_WHITESPACE = re.compile(r"\s+")

# Arabic-Indic (٠-٩) and Extended/Persian Arabic-Indic (۰-۹) digits.
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})

_ORTHOGRAPHIC_MAP = {
    ord("أ"): "ا", ord("إ"): "ا", ord("آ"): "ا", ord("ٱ"): "ا",
    ord("ة"): "ه",
    ord("ى"): "ي",
}

# The default both callers read. Flip this in one place to A/B the choice
# across index and query together — never flip it on one side only.
FOLD_ORTHOGRAPHY_DEFAULT = True


def normalize_arabic(text: str, fold_orthography: bool = None) -> str:
    """
    Normalize Arabic text for embedding.

    Applied to the text that gets EMBEDDED, never to the text that gets
    STORED. chunks.json keeps the original readable Arabic, because that is
    what goes into the generation prompt and what a human proofreads —
    folding 'ة' to 'ه' in what the model reads would degrade the answer to
    buy nothing.
    """
    if not text:
        return ""

    if fold_orthography is None:
        fold_orthography = FOLD_ORTHOGRAPHY_DEFAULT

    # NFKC first: presentation forms must become real letters BEFORE the
    # letter-level maps below run, or those maps silently miss them.
    text = unicodedata.normalize("NFKC", text)

    text = _INVISIBLE.sub("", text)
    text = _TASHKEEL.sub("", text)
    text = text.replace(_TATWEEL, "")
    text = text.translate(_DIGIT_MAP)

    if fold_orthography:
        text = text.translate(_ORTHOGRAPHIC_MAP)

    return _WHITESPACE.sub(" ", text).strip()
