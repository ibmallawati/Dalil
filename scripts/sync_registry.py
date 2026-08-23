"""
sync_registry.py — make every corpus file routable.

    python scripts/sync_registry.py            # show what would change
    python scripts/sync_registry.py --write    # apply it

Reads data/source_docs/*.md and ensures every authority and service they
name is declared in data/registry/authorities.json.

Run this after dropping a hand-written markdown file into the corpus.
scripts/import_ocr_corpus.py already calls it for you.

--- Why this is needed at all --------------------------------------------

L1 refuses an unknown authority BEFORE retrieval runs. A corpus file whose
authority is not declared indexes perfectly and is then unreachable
forever, and the only symptom is a refusal that looks deliberate.
build_index.py prints UNROUTABLE when it detects this; this script fixes it.

What it generates is deliberately minimal — a formal name and nothing else.
It cannot invent the phrasings that make L1 work («بطاقتي», «رخصتي
منتهية»), so it says so loudly and leaves that pass to a human.
"""

import argparse
import io
import sys
from pathlib import Path

from chatbot.config import DATA_DIR
from chatbot.rag.registry_sync import RegistrySyncError, format_report, sync

CORPUS_DIR = DATA_DIR / "source_docs"
REGISTRY = DATA_DIR / "registry" / "authorities.json"


def _force_utf8_stdout():
    """Windows consoles default to cp1252 and cannot print Arabic."""
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace")


def main():
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Declare every corpus authority in the registry.")
    parser.add_argument("--write", action="store_true",
                        help="apply the changes (default is a dry run)")
    parser.add_argument("--corpus-dir", type=Path, default=CORPUS_DIR)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    args = parser.parse_args()

    try:
        report = sync(args.corpus_dir, args.registry, write=args.write)
    except RegistrySyncError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)

    print(format_report(report, wrote=args.write))

    if not args.write and (report["added"] or report["services_added"]):
        print("\nRe-run with --write to apply.")
    if args.write and report["added"]:
        # An automatic change to what Daleel claims to cover is worth a
        # non-zero exit: it makes CI or a shell prompt notice.
        sys.exit(2)


if __name__ == "__main__":
    main()
