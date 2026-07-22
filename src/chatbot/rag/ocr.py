"""
ocr_extract.py -- hybrid OCR component (Phase 6)

For each PDF page:
  1. try pypdf text-layer extraction (fast, accurate, free)
  2. if that text is empty / too short, the page is probably a scan
  3. render the page to an image and run Tesseract (eng+ara)

Runs at INDEX-BUILD time only (inside build_index.py). Tesseract and poppler
never ship to Render.

Local system dependencies (install on the build machine, NOT in the Dockerfile):
  Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara poppler-utils
  macOS:         brew install tesseract tesseract-lang poppler

Python deps (build-only, see requirements-build.txt): pytesseract, pdf2image
"""
from pypdf import PdfReader
from pdf2image import convert_from_path
import pytesseract

# Below this many non-space chars, treat a page as "no text layer" -> OCR it.
MIN_CHARS = 20
OCR_LANG = "eng+ara"   # languages Tesseract loads
OCR_DPI = 300          # render resolution for scanned pages (300 = OCR sweet spot)


def _pypdf_text(pdf_path, page_index):
    reader = PdfReader(pdf_path)
    return (reader.pages[page_index].extract_text() or "").strip()


def _ocr_text(pdf_path, page_index):
    images = convert_from_path(
        pdf_path, dpi=OCR_DPI,
        first_page=page_index + 1, last_page=page_index + 1,  # 1-based
    )
    if not images:
        return ""
    return pytesseract.image_to_string(images[0], lang=OCR_LANG).strip()


def _looks_empty(text):
    return len(text.replace(" ", "").replace("\n", "")) < MIN_CHARS


def extract_text_hybrid(pdf_path, verbose=True):
    """
    Returns a list of per-page dicts: {page, source, chars, text}
    source is 'text_layer' or 'ocr'.
    """
    reader = PdfReader(pdf_path)
    results = []
    for i in range(len(reader.pages)):
        text = _pypdf_text(pdf_path, i)
        source = "text_layer"
        if _looks_empty(text):
            ocr = _ocr_text(pdf_path, i)
            if len(ocr) > len(text):
                text, source = ocr, "ocr"
        results.append({"page": i + 1, "source": source,
                        "chars": len(text), "text": text})
        if verbose:
            print(f"  page {i+1}: {source:10s} ({len(text)} chars)")
    return results
