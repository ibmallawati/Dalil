"""
ocr.py -- PDF pages to text, via Tesseract.

EVERY page is rendered to an image and OCR'd. A PDF's embedded text layer is
never read, even when it has a perfectly good one.

Runs at INDEX-BUILD time only. Tesseract and poppler never ship to Render.

--- Why the text layer is ignored ----------------------------------------

Reading it is faster, and for these documents it was wrong. pypdf returned
Arabic in which U+064B (fathatan) stood in for the space character --
measured 4,663 fathatan against 3,721 real spaces. That text is long, so any
"does this page have text?" check passes it. D3 normalisation then strips
tashkeel by design and every word in the document fuses together. No
exception, no warning, just degraded retrieval.

This module previously detected that case and fell back to OCR per page.
It worked, but it meant two extraction paths and a heuristic choosing
between them -- and the heuristic was wrong on its first version, flagging
vocalised Arabic («بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ») as corrupt because
genuine vocalisation carries more diacritics than spaces.

One path cannot silently pick the wrong one. Whatever needs correcting is
corrected in ONE place downstream -- the cleaner in docs/PDF_INTAKE_SPEC.md
-- rather than split between a detector here and a cleaner there.

Cost, stated plainly: slower, and a page that genuinely had good digital
text is re-derived from an image, so it can pick up an OCR error it would
not otherwise have had. Acceptable because these documents are
overwhelmingly scans, and because the alternative is a silent corruption
that is much harder to notice than a visible OCR typo.

Local system dependencies (build machine only, NOT the Dockerfile):
  Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara poppler-utils
  macOS:         brew install tesseract tesseract-lang poppler
  Windows:       winget install UB-Mannheim.TesseractOCR oschwartz10612.Poppler

Python deps (build-only, see requirements-build.txt): pytesseract, pdf2image
"""
from pypdf import PdfReader
from pdf2image import convert_from_path
import pytesseract

OCR_LANG = "eng+ara"   # languages Tesseract loads
OCR_DPI = 300          # render resolution (300 = OCR sweet spot)


def _page_count(pdf_path):
    """pypdf is used ONLY to count pages -- never to read text."""
    return len(PdfReader(pdf_path).pages)


def _ocr_text(pdf_path, page_index):
    """
    Render one page and OCR it.

    Rendered one page at a time on purpose: a 300-DPI bitmap of a whole
    document held in memory at once is how this falls over on a long PDF.
    """
    images = convert_from_path(
        pdf_path, dpi=OCR_DPI,
        first_page=page_index + 1, last_page=page_index + 1,  # 1-based
    )
    if not images:
        return ""
    return pytesseract.image_to_string(images[0], lang=OCR_LANG).strip()


def extract_text(pdf_path, verbose=True):
    """
    OCR every page. Returns a list of {page, chars, text}, page 1-based.

    A page that yields nothing is kept as an empty record rather than
    dropped, so page numbers stay aligned with the document -- citations
    built from them have to point at the right page.
    """
    total = _page_count(pdf_path)
    results = []
    for i in range(total):
        text = _ocr_text(pdf_path, i)
        results.append({"page": i + 1, "chars": len(text), "text": text})
        if verbose:
            blank = "  <- no text found" if not text else ""
            print(f"  page {i + 1}/{total}: {len(text)} chars{blank}")
    return results
