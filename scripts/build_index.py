"""
Build the embedding index (run OFFLINE, whenever the corpus changes).

PRIMARY ROUTE - curated Arabic markdown:
    python scripts/build_index.py

  - reads every .md in data/source_docs/ (see docs/CORPUS_SPEC.md),
  - validates the YAML front-matter and fails loudly on a bad file,
  - splits each file on '##' headers, one chunk per section,
  - embeds each chunk LOCALLY via chatbot.rag.retrieval.embed_passages, so
    the corpus and the queries go through the SAME model, the SAME D3
    normalization, and the correct E5 "passage:" prefix,
  - writes data/index/embeddings.npy + data/index/chunks.json.

DEVELOPMENT against fixtures (no real corpus needed):
    python scripts/build_index.py --fixtures

  Builds data/fixtures/ into data/fixtures/index/. Fixtures are invented
  stand-in content and can NEVER be written to the real index path - see
  the guard in resolve_paths(). No number produced from a fixture index may
  be quoted as a result.

SECONDARY ROUTE - PDF + OCR, kept as the documented path to scale:
    python scripts/build_index.py --pdf

  Requires the build extras (pip install -e ".[build]") plus local
  tesseract/poppler. See src/chatbot/rag/ocr.py.

Embedding runs locally in-process. No API, no key, no network, no cost.
The app then LOADS the two artifacts at startup and only ever embeds the
incoming query.
"""

import argparse
import glob
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np

from chatbot.config import settings, DATA_DIR
from chatbot.logging_config import configure_logging
from chatbot.rag.markdown_corpus import CorpusError, load_corpus
from chatbot.rag.retrieval import embed_passages

logger = logging.getLogger(__name__)

FIXTURES_DIR = DATA_DIR / "fixtures"
FIXTURES_INDEX_DIR = FIXTURES_DIR / "index"

# Legacy character-window settings, used ONLY by the PDF route. The markdown
# route does not chunk by characters - see markdown_corpus for why.
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100


# --- PDF/OCR route (secondary) -------------------------------------------

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    text = " ".join(text.split())
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return chunks


def build_records_from_pdfs(folder):
    """The Phase 6 hybrid text-layer/OCR route, retained for scale."""
    from chatbot.rag.ocr import extract_text_hybrid

    records = []
    page_source_counts = {"text_layer": 0, "ocr": 0}
    for path in glob.glob(os.path.join(str(folder), "*.pdf")):
        filename = os.path.basename(path)
        print(f"Extracting {filename} ...")
        for page in extract_text_hybrid(path):
            page_source_counts[page["source"]] += 1
            for chunk in chunk_text(page["text"]):
                if chunk.strip():
                    records.append({
                        "text": chunk,
                        "embed_text": chunk,
                        "source": filename,
                        "page": page["page"],
                        "extraction_source": page["source"],
                        "fixture": False,
                    })
    print(
        f"OCR summary: {page_source_counts['text_layer']} pages via text_layer, "
        f"{page_source_counts['ocr']} pages via ocr"
    )
    return records


# --- Paths + the fixture guard -------------------------------------------

def resolve_paths(args):
    """
    Decide where to read from and write to, and refuse the one combination
    that would be dangerous.
    """
    if args.fixtures:
        source_dir = Path(args.source_dir) if args.source_dir else FIXTURES_DIR
        out_dir = Path(args.out_dir) if args.out_dir else FIXTURES_INDEX_DIR
    else:
        source_dir = Path(args.source_dir) if args.source_dir else settings.source_docs_dir
        out_dir = Path(args.out_dir) if args.out_dir else settings.index_file.parent
    return source_dir, out_dir


def assert_fixtures_quarantined(records, out_dir):
    """
    Hard stop: fixture content must never land in the real index.

    Fixtures are invented. If they reached data/index/ the app would answer
    citizens from made-up government procedures, and any accuracy figure
    measured against them would be meaningless while looking legitimate.
    Cheap check, catastrophic failure mode.
    """
    real_index_dir = settings.index_file.parent.resolve()
    if out_dir.resolve() != real_index_dir:
        return
    fixtures = sorted({r.get("source", "?") for r in records if r.get("fixture")})
    if fixtures:
        raise SystemExit(
            "REFUSING TO BUILD: fixture content cannot be written to the real "
            f"index at {real_index_dir}.\n"
            f"  Offending files: {', '.join(fixtures)}\n"
            "  Fixtures are invented stand-in content. Use --fixtures to build "
            "them into data/fixtures/index/ instead."
        )


def report_registry_coverage(corpus_service_ids):
    """
    Reconcile the corpus against the registry, in both directions.

    These two files are joined only by the service_id string, and drift
    between them is silent: the app keeps running and simply refuses.
    Printing the comparison at build time makes the state of the corpus
    visible at the moment someone can act on it.
    """
    try:
        from chatbot.agent import registry
    except Exception as exc:      # registry file missing/broken: not fatal here
        print(f"\n(could not read the registry: {exc})")
        return

    declared = {s for a in registry.coverage_summary()["authorities"]
                for s in a["services"]}
    have = set(corpus_service_ids)

    unroutable = sorted(have - declared)
    missing = sorted(declared - have)

    print(f"\nregistry check: {len(have & declared)}/{len(declared)} declared "
          f"services have content")

    if unroutable:
        # Content nothing can ever reach. Always a bug worth naming.
        print("  UNROUTABLE — indexed but not declared in the registry, so no")
        print("  question can ever be routed to it:")
        for s in unroutable:
            print(f"    - {s}")
        print("  Fix: add it to data/registry/authorities.json, or correct the")
        print("  service_id so it matches an existing entry.")

    if missing:
        # Expected while curating: the registry declares the target scope
        # and the corpus fills in behind it.
        print("  NO CONTENT YET — declared in the registry, no file in the")
        print("  corpus. These correctly answer with a refusal until curated:")
        for s in missing:
            print(f"    - {s}")

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixtures", action="store_true",
                        help="build data/fixtures/ into data/fixtures/index/")
    parser.add_argument("--pdf", action="store_true",
                        help="use the legacy PDF+OCR route instead of markdown")
    parser.add_argument("--source-dir", default=None)
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    configure_logging()
    source_dir, out_dir = resolve_paths(args)

    print(f"source : {source_dir}")
    print(f"output : {out_dir}")
    print(f"model  : {settings.embed_model} ({settings.embed_dim}d)")
    print("-" * 66)

    if args.pdf:
        records = build_records_from_pdfs(source_dir)
    else:
        try:
            records = load_corpus(source_dir)
        except CorpusError as exc:
            # A malformed curated file is a content bug someone must fix,
            # not something to build around.
            print(f"\nCORPUS ERROR: {exc}", file=sys.stderr)
            return 1

    if not records:
        print(f"\nNo content found in {source_dir}/.")
        print("The markdown corpus is curated by hand - see docs/CORPUS_SPEC.md.")
        return 1

    assert_fixtures_quarantined(records, out_dir)

    services = sorted({r.get("service_id", r.get("source", "?")) for r in records})
    print(f"{len(records)} chunks from {len(services)} service file(s):")
    for s in services:
        n = sum(1 for r in records if r.get("service_id", r.get("source")) == s)
        print(f"  {n:3d}  {s}")

    report_registry_coverage(services)

    print(f"\nEmbedding {len(records)} chunks locally ...")
    # embed_passages applies D3 normalization and the E5 "passage: " prefix.
    # It is the SAME entry point the query path uses, which is what keeps
    # the two sides of retrieval from drifting apart.
    vectors = embed_passages([r["embed_text"] for r in records])

    if vectors.shape[1] != settings.embed_dim:
        print(f"\nWARNING: model produced {vectors.shape[1]}d vectors but "
              f"EMBED_DIM={settings.embed_dim}. Fix EMBED_DIM or the app will "
              f"reject this index at startup.", file=sys.stderr)

    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / settings.index_file.name
    chunks_path = out_dir / settings.chunks_file.name

    np.save(index_path, vectors)
    # embed_text is derivable and only bloats the artifact; drop it.
    stored = [{k: v for k, v in r.items() if k != "embed_text"} for r in records]
    with open(chunks_path, "w", encoding="utf-8") as f:
        json.dump(stored, f, ensure_ascii=False)

    print(f"\nSaved {index_path} {vectors.shape}")
    print(f"Saved {chunks_path} ({len(stored)} records)")
    if any(r.get("fixture") for r in records):
        print("\n*** FIXTURE INDEX - invented content. No number measured "
              "against this may be quoted as a result. ***")
    return 0


if __name__ == "__main__":
    sys.exit(main())
