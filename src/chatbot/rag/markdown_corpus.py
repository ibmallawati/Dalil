"""
markdown_corpus.py — load and validate Daleel's curated Arabic corpus.

Daleel's primary ingestion route is hand-curated Arabic markdown, one file
per government service. This module turns those files into chunks; it does
NOT embed anything (scripts/build_index.py does that).

--- Why markdown and not PDF ---------------------------------------------

The D3 pre-flight gate found that real Arabic government PDFs extract
badly and inconsistently: 63-80% of Arabic characters came back as glyph
presentation forms with the text stored in visual (reversed) order, and the
failure mode varies by PDF producer, so it cannot be predicted per file.
Curated markdown removes that entire class of corruption. The PDF/OCR route
remains in the repo as the documented path to scale.

--- Chunking: one '##' section, one chunk -------------------------------

The old character-window chunker (500 chars / 100 overlap) is deliberately
NOT ported. It sliced fee tables and step lists in half, producing chunks
that were individually meaningless. A curated '##' section is already the
right unit: it is one topic, written to be read on its own.

Each chunk carries its document's front-matter, so every retrieved snippet
knows which authority and service it came from and — critically for D5 —
which source_url to cite.

--- The context prefix ---------------------------------------------------

A section headed «الرسوم» ("fees") is unretrievable on its own: nothing in
its text says which service's fees these are. So the text that gets EMBEDDED
is prefixed with its document context:

    وزارة | خدمة | ## القسم
    <section body>

while the text that gets STORED stays clean, because that is what goes into
the generation prompt and what a human proofreads.
"""

import logging
import re
from pathlib import Path

import yaml   # build-time only; nothing at runtime parses YAML

logger = logging.getLogger(__name__)

# Front-matter fields every curated file must carry. See docs/CORPUS_SPEC.md
# — that document is the contract these names come from.
REQUIRED_FIELDS = (
    "authority",       # Arabic name of the authority
    "authority_en",    # English/Latin name, for logs and the panel
    "service",         # Arabic name of the service
    "service_id",      # stable slug, e.g. rop-passport-renewal
    "source_url",      # D5 cites THIS, never the filename
    "source_title",    # title of the official page, as published
    "retrieved",       # ISO date the content was taken from source
    "verified_by",     # who checked it against the official page
)

# A curated file whose facts nobody has checked must not reach a demo
# silently. Values that mean "not yet verified".
UNVERIFIED_MARKERS = {"", "pending", "todo", "unverified", "unverified_example", "-"}

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# A leading HTML comment is allowed before the front-matter, and only that.
# Fixtures and the worked example carry a warning banner there; the comment
# is invisible when the markdown renders, so the warning cannot be lost by
# someone reading the file in a previewer. Anything ELSE before the '---'
# is still an error, because the usual causes (a stray blank line, a BOM, a
# heading written above the metadata) are real mistakes worth catching.
_LEADING_COMMENT = re.compile(r"\A\s*(?:<!--.*?-->\s*)*", re.DOTALL)

# e5 truncates silently past its max sequence length. A section far beyond
# that loses its tail with no error — the same class of invisible failure
# the D3 gate was built to catch, so it is surfaced at build time.
SECTION_WORD_WARN = 350


class CorpusError(Exception):
    """A curated file is malformed. Fail the build loudly, never silently."""


def parse_front_matter(text, path_label="<string>"):
    """Split a markdown file into (front_matter dict, body str)."""
    text = text.lstrip("﻿")                  # strip a BOM if an editor added one
    text = text[_LEADING_COMMENT.match(text).end():]

    match = _FRONT_MATTER.match(text)
    if not match:
        raise CorpusError(
            f"{path_label}: no YAML front-matter. The file must start with a "
            f"'---' line (an HTML comment may precede it, nothing else). "
            f"See docs/CORPUS_SPEC.md."
        )
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise CorpusError(f"{path_label}: front-matter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise CorpusError(f"{path_label}: front-matter must be key/value pairs.")
    return meta, text[match.end():]


def validate_front_matter(meta, path_label="<string>"):
    """
    Check every required field is present and non-empty.

    Validation exists because the corpus is hand-curated by several people
    under deadline. Catching a missing source_url at BUILD time costs
    seconds; discovering it at demo time — when D5 has no source to cite —
    costs the answer's credibility in front of judges.
    """
    missing = [f for f in REQUIRED_FIELDS if not str(meta.get(f, "")).strip()]
    if missing:
        raise CorpusError(
            f"{path_label}: missing required front-matter field(s): "
            f"{', '.join(missing)}. See docs/CORPUS_SPEC.md."
        )

    url = str(meta["source_url"]).strip()
    if not url.startswith(("http://", "https://")):
        raise CorpusError(
            f"{path_label}: source_url must be a full URL (D5 cites it "
            f"directly to the citizen), got: {url!r}"
        )
    return meta


def split_sections(body, path_label="<string>"):
    """
    Split a markdown body on '##' headers. Returns [(heading, text), ...].

    Content before the first '##' is kept under the document's '#' title
    rather than dropped, so an intro paragraph is never silently lost.
    """
    title_match = _H1.search(body)
    doc_title = title_match.group(1).strip() if title_match else None

    parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE)
    # re.split with one capture group -> [pre, head1, body1, head2, body2, ...]
    preamble = parts[0]
    if _H1.search(preamble):
        preamble = _H1.sub("", preamble, count=1)

    sections = []
    if preamble.strip():
        sections.append((doc_title or "عام", preamble.strip()))

    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        text = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if not text:
            logger.warning("corpus_empty_section",
                           extra={"file": path_label, "section": heading})
            continue
        sections.append((heading, text))

    if not sections:
        raise CorpusError(f"{path_label}: no content found (no '##' sections).")
    return sections


def build_embed_text(meta, heading, text):
    """
    The text that gets EMBEDDED — document context plus the section.

    Without the authority/service prefix a section headed «الرسوم» carries
    nothing that identifies which service's fees it lists, and is therefore
    effectively unretrievable.
    """
    return f"{meta['authority']} | {meta['service']} | {heading}\n{text}"


def load_file(path):
    """Turn one curated markdown file into a list of chunk dicts."""
    path = Path(path)
    label = path.name
    raw = path.read_text(encoding="utf-8")

    meta, body = parse_front_matter(raw, label)
    validate_front_matter(meta, label)

    is_fixture = bool(meta.get("fixture", False))
    verified = str(meta.get("verified_by", "")).strip().lower()
    if verified in UNVERIFIED_MARKERS and not is_fixture:
        logger.warning(
            "corpus_unverified_file",
            extra={"file": label, "verified_by": meta.get("verified_by"),
                   "hint": "nobody has checked these facts against the official page"},
        )

    chunks = []
    for heading, text in split_sections(body, label):
        words = len(text.split())
        if words > SECTION_WORD_WARN:
            logger.warning(
                "corpus_section_too_long",
                extra={"file": label, "section": heading, "words": words,
                       "hint": "e5 truncates silently; split this into more '##' sections"},
            )
        chunks.append({
            # Stored text stays clean readable Arabic: this is what reaches
            # the generation prompt and what a human proofreads.
            "text": text,
            "embed_text": build_embed_text(meta, heading, text),
            "section": heading,
            "authority": meta["authority"],
            "authority_en": meta["authority_en"],
            "service": meta["service"],
            "service_id": meta["service_id"],
            # D5's source_document cites source_url, never the filename.
            "source_url": meta["source_url"],
            "source_title": meta["source_title"],
            "retrieved": str(meta["retrieved"]),
            "verified_by": meta["verified_by"],
            "source": label,          # kept for logs/debugging only
            "fixture": is_fixture,
        })
    return chunks


# Documentation living alongside the corpus is not corpus. Skipped by name
# rather than by "does it have front-matter?", so a REAL service file with
# broken front-matter still fails loudly instead of being quietly ignored.
SKIP_FILENAMES = {"readme.md", "index.md", "notes.md"}


def is_corpus_file(path):
    name = Path(path).name.lower()
    return name not in SKIP_FILENAMES and not name.startswith("_")


def load_corpus(folder):
    """
    Load every corpus .md file in `folder`. Raises CorpusError on the first
    bad file — a partially-valid corpus is not something to build an index
    from, and a silently-skipped service is a service Daleel will deny
    having while the file sits right there.
    """
    folder = Path(folder)
    paths = [p for p in sorted(folder.glob("*.md")) if is_corpus_file(p)]
    chunks = []
    for path in paths:
        file_chunks = load_file(path)
        logger.info("corpus_file_loaded",
                    extra={"file": path.name, "sections": len(file_chunks),
                           "service_id": file_chunks[0]["service_id"]})
        chunks.extend(file_chunks)
    return chunks
