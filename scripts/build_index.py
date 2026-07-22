"""
Build the embedding index (run OFFLINE, once, whenever the PDFs change).

What it does:
- reads every PDF in data/source_docs/,
- chunks each page (same 500/100 settings as before),
- embeds each chunk via the HF API (reusing chatbot.rag.retrieval.embed, so
  the corpus and the queries are embedded exactly the same way),
- writes data/index/embeddings.npy + data/index/chunks.json.

The app then just LOADS those two files at startup - it never re-embeds the
corpus, so runtime stays fast and light with no torch.

Usage (from the repo root, with the package installed - `pip install -e .`):
    python scripts/build_index.py

Then commit embeddings.npy and chunks.json to your repo so the deployed app
has them. Re-run this whenever you add or change a PDF.

Setup: pip install pypdf huggingface_hub numpy
Requires HF_TOKEN in your environment (or .env).

OCR fallback (Phase 6): pages with no usable text layer (scanned PDFs, or
scanned pages inside an otherwise-digital PDF) are run through Tesseract OCR
via chatbot.rag.ocr. That's a BUILD-ONLY dependency:
  pip install -r requirements-build.txt
and it needs these installed locally (NOT in the Dockerfile):
  Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara poppler-utils
  macOS:         brew install tesseract tesseract-lang poppler
"""

import os
import glob
import json
import numpy as np

# Reuse the SAME embed() the app uses, so corpus and query vectors match.
from chatbot.config import settings
from chatbot.rag.retrieval import embed
from chatbot.rag.ocr import extract_text_hybrid


CHUNK_SIZE = 500
CHUNK_OVERLAP = 100


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    text = " ".join(text.split())
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return chunks


def build_records(folder=None):
    folder = folder or settings.source_docs_dir
    records = []
    page_source_counts = {"text_layer": 0, "ocr": 0}
    for path in glob.glob(os.path.join(folder, "*.pdf")):
        filename = os.path.basename(path)
        print(f"Extracting {filename} ...")
        pages = extract_text_hybrid(path)
        for page in pages:
            page_source_counts[page["source"]] += 1
            for chunk in chunk_text(page["text"]):
                if chunk.strip():
                    records.append({
                        "text": chunk,
                        "source": filename,
                        "page": page["page"],
                        "extraction_source": page["source"],
                    })
    return records, page_source_counts


def main():
    records, page_source_counts = build_records()
    if not records:
        print(f"No text found. Put PDF files in the '{settings.source_docs_dir}/' folder.")
        return

    print(
        f"OCR summary: {page_source_counts['text_layer']} pages via text_layer, "
        f"{page_source_counts['ocr']} pages via ocr"
    )

    print(f"Embedding {len(records)} chunks via the HF API ...")
    vectors = np.stack([embed(r["text"]) for r in records]).astype(np.float32)

    settings.index_file.parent.mkdir(parents=True, exist_ok=True)
    np.save(settings.index_file, vectors)
    with open(settings.chunks_file, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)

    print(f"Saved {settings.index_file} {vectors.shape} and {settings.chunks_file} ({len(records)} records).")
    print("Commit both files so the deployed app can load them.")


if __name__ == "__main__":
    main()
