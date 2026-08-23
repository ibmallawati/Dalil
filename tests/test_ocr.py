"""
OCR extraction tests.

No real PDFs and no Tesseract — Tesseract is faked, so what is under test is
the extraction CONTRACT: every page is OCR'd, the text layer is never read,
and page numbers stay aligned with the document.

That first property is the one worth guarding. Reading a PDF's embedded text
when it has one is an obvious optimisation, and on these documents it is
silently wrong: the ministry PDFs carried Arabic with U+064B in place of the
space character, which is long enough to look healthy and fuses into a
single word once D3 strips tashkeel.

An earlier version detected that and fell back to OCR. It also, in its first
form, misread vocalised Arabic as corrupt. The detector is gone; these tests
exist so it does not come back by accident.
"""

import pytest

from chatbot.rag import ocr


class ExplodingPage:
    """A page whose text layer cannot be read without failing the test."""

    def extract_text(self):
        raise AssertionError(
            "the PDF text layer was read — it must never be trusted; "
            "see chatbot.rag.ocr"
        )


class FakeReader:
    def __init__(self, page_count):
        self.pages = [ExplodingPage() for _ in range(page_count)]


@pytest.fixture
def fake_pdf(monkeypatch):
    """A 3-page PDF whose text layer detonates, and predictable OCR."""
    ocred = []

    def fake_ocr(pdf_path, page_index):
        ocred.append(page_index)
        return f"نص الصفحة {page_index + 1}"

    monkeypatch.setattr(ocr, "PdfReader", lambda path: FakeReader(3))
    monkeypatch.setattr(ocr, "_ocr_text", fake_ocr)
    return ocred


def test_every_page_is_ocred(fake_pdf):
    pages = ocr.extract_text("anything.pdf", verbose=False)
    assert [p["text"] for p in pages] == [
        "نص الصفحة 1", "نص الصفحة 2", "نص الصفحة 3"]
    assert fake_pdf == [0, 1, 2], "every page index must reach OCR"


def test_the_text_layer_is_never_read(fake_pdf):
    """
    Guarded by ExplodingPage rather than by a spy: an assertion inside the
    fake fires at the moment of the mistake, so a future optimisation that
    reads the layer fails here instead of quietly changing behaviour.
    """
    ocr.extract_text("anything.pdf", verbose=False)   # must not raise


def test_pages_are_numbered_from_one(fake_pdf):
    """Citations are built from these numbers, so an off-by-one misattributes."""
    assert [p["page"] for p in ocr.extract_text("x.pdf", verbose=False)] == [1, 2, 3]


def test_a_blank_page_is_kept_not_dropped(monkeypatch):
    """
    Dropping an empty page would shift every later page number by one, and
    the citation would then point at the wrong page of the source document.
    """
    monkeypatch.setattr(ocr, "PdfReader", lambda path: FakeReader(3))
    monkeypatch.setattr(ocr, "_ocr_text",
                        lambda path, i: "" if i == 1 else f"نص {i + 1}")

    pages = ocr.extract_text("x.pdf", verbose=False)
    assert [p["page"] for p in pages] == [1, 2, 3]
    assert pages[1]["text"] == "" and pages[1]["chars"] == 0


def test_chars_reports_the_extracted_length(fake_pdf):
    pages = ocr.extract_text("x.pdf", verbose=False)
    assert all(p["chars"] == len(p["text"]) for p in pages)
