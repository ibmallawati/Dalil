"""
ocr_inbox.py — turn PDFs into OCR text you can read.

    # 1. drop PDFs into data/pdf_inbox/
    # 2. run:
    python scripts/ocr_inbox.py

Each `data/pdf_inbox/<name>.pdf` becomes `data/pdf_ocr/<name>.ocr.txt`.

--- What this does NOT do ------------------------------------------------

It does not add anything to the chatbot's knowledge. It stops at text.

Turning that text into indexed, citable services is a separate job with
real decisions attached — OCR recovers no usable URLs from these documents,
and importing a ministry the registry lists as `not_covered` leaves it
indexed but unreachable. See docs/PDF_INTAKE_SPEC.md.

Stopping here is deliberate: you can read the output and judge whether a
PDF is worth importing before anyone builds the importer for it.

--- Every page is OCR'd --------------------------------------------------

Even pages that already contain digital text. Reading that text would be
faster, but on the ministry PDFs it was silently corrupt -- U+064B standing
in for the space character, long enough that any "does this page have text?"
check passes it, after which D3's tashkeel stripping fuses every word in the
document.

One extraction path means no heuristic that can pick the wrong one.
Correction happens once, downstream, in the cleaner. See chatbot.rag.ocr.
"""

import argparse
import io
import shutil
import sys
from pathlib import Path

from chatbot.config import DATA_DIR


def _force_utf8_stdout():
    """
    Windows consoles default to cp1252 and cannot print Arabic. Done in
    main() rather than at import, so importing this module does not hijack
    another program's stdout.
    """
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace")

INBOX = DATA_DIR / "pdf_inbox"
OUT_DIR = DATA_DIR / "pdf_ocr"
SUFFIX = ".ocr.txt"

# Page separator. The importer reads these back to build page-number
# citations, since these documents give us nothing else to cite.
PAGE_MARKER = "===== PAGE {n} ====="


def check_toolchain():
    """
    Fail with an instruction, not a traceback.

    A missing binary surfaces from deep inside pytesseract/pdf2image as
    something unhelpful, and the fix is one install command.
    """
    problems = []
    if not shutil.which("tesseract"):
        problems.append(
            "tesseract not found on PATH.\n"
            "    Windows: winget install UB-Mannheim.TesseractOCR\n"
            "    macOS:   brew install tesseract tesseract-lang\n"
            "    Debian:  sudo apt install tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng"
        )
    else:
        try:
            import pytesseract
            langs = pytesseract.get_languages(config="")
            missing = [lang for lang in ("ara", "eng") if lang not in langs]
            if missing:
                problems.append(
                    f"tesseract is installed but missing language data: {', '.join(missing)}.\n"
                    "    Arabic is not optional here — install tesseract-ocr-ara."
                )
        except Exception as exc:                      # noqa: BLE001
            problems.append(f"could not query tesseract languages: {exc}")

    if not (shutil.which("pdftoppm") or shutil.which("pdftocairo")):
        problems.append(
            "poppler not found on PATH (needed to render pages for OCR).\n"
            "    Windows: winget install oschwartz10612.Poppler\n"
            "    macOS:   brew install poppler\n"
            "    Debian:  sudo apt install poppler-utils"
        )

    if problems:
        print("Cannot run OCR:\n")
        for problem in problems:
            print(f"  - {problem}\n")
        sys.exit(1)


def ocr_one(pdf_path, out_path):
    """OCR a single PDF and write the page-marked text. Returns a summary."""
    from chatbot.rag.ocr import extract_text

    pages = extract_text(str(pdf_path), verbose=True)

    body = "\n".join(
        f"\n{PAGE_MARKER.format(n=page['page'])}\n{page['text']}"
        for page in pages
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")

    return {
        "pages": len(pages),
        "chars": sum(page["chars"] for page in pages),
        "empty_pages": sum(1 for page in pages if page["chars"] == 0),
    }


def main():
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="OCR every PDF in data/pdf_inbox/ into data/pdf_ocr/.")
    parser.add_argument("pdfs", nargs="*", type=Path,
                        help="specific PDFs; default is everything in the inbox")
    parser.add_argument("--force", action="store_true",
                        help="re-OCR even if the .ocr.txt already exists")
    parser.add_argument("--inbox", type=Path, default=INBOX)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    check_toolchain()

    args.inbox.mkdir(parents=True, exist_ok=True)
    pdfs = args.pdfs or sorted(args.inbox.glob("*.pdf"))

    if not pdfs:
        print(f"No PDFs found in {args.inbox}")
        print("Drop .pdf files there and run this again.")
        return

    print(f"inbox : {args.inbox}")
    print(f"output: {args.out_dir}")
    print(f"found : {len(pdfs)} PDF(s)\n")

    written, skipped, failed = [], [], []
    for pdf in pdfs:
        out_path = args.out_dir / (pdf.stem + SUFFIX)
        if out_path.exists() and not args.force:
            print(f"SKIP  {pdf.name} — {out_path.name} exists (use --force)")
            skipped.append(pdf.name)
            continue

        print(f"OCR   {pdf.name}")
        try:
            summary = ocr_one(pdf, out_path)
        except Exception as exc:                       # noqa: BLE001
            # One bad PDF must not abandon the rest of the batch.
            print(f"  FAILED: {type(exc).__name__}: {exc}\n")
            failed.append((pdf.name, str(exc)))
            continue

        print(f"  -> {out_path.name}: {summary['pages']} pages, "
              f"{summary['chars']:,} chars")
        if summary["empty_pages"]:
            print(f"  !  {summary['empty_pages']} page(s) produced NO text — "
                  f"likely images without legible text")
        print()
        written.append(pdf.name)

    print("=" * 60)
    print(f"written: {len(written)}   skipped: {len(skipped)}   failed: {len(failed)}")
    for name, error in failed:
        print(f"  FAILED {name}: {error}")
    if written:
        print(f"\nRead the output in {args.out_dir} before importing anything.")
        print("Nothing has been added to the chatbot's knowledge — see "
              "docs/PDF_INTAKE_SPEC.md for why that is a separate step.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
