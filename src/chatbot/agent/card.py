"""
card.py — answer provenance and the confidence vocabulary.

--- What this used to be, and why it changed -----------------------------

This module used to define a FIXED six-field answer card (eligibility,
documents, steps, fees, duration, where) plus a post-pass forcing every
unsupported field to «غير محدد في المصدر».

That was right for a corpus of six near-identical ROP procedures. It is
wrong for a broad, heterogeneous corpus:

  - a citizen asking «كم الرسوم؟» got six sections, five unasked-for;
  - source material that did not fit the seven headings was lost at INGEST
    time, because the ingest step existed to force it into them;
  - every gap rendered as «غير محدد في المصدر», so a thin service looked
    like a broken answer rather than a short one.

The answer shape now follows the QUESTION, not a schema. What survives here
is the part that was never about layout:

  1. provenance comes from the corpus, never from the model
  2. confidence is a closed vocabulary, and anything unrecognised is "none"

--- Provenance is still not the model's to invent ------------------------

A hallucinated citation is worse than no citation, because it survives a
glance. Source fields are read off the retrieved chunks and overwrite
whatever the model emitted.

Retrieval can now span several services in one answer — a question about
two fees legitimately pulls from two files — so sources are a LIST. A
single-source answer is just the one-element case.
"""

# The model reports this about its own grounding:
#   high    — the retrieved text answers the question
#   partial — it answers some of it
#   none    — it does not contain the answer; do not invent one
CONFIDENCE_VALUES = ("high", "partial", "none")


def normalise_confidence(value):
    """
    An unreadable or absent confidence is not evidence of grounding, so it
    degrades to "none" rather than being ignored.
    """
    return value if value in CONFIDENCE_VALUES else "none"


def _source_of(chunk):
    """One source entry, as recorded in the corpus at ingest time."""
    url = (chunk.get("source_url") or "").strip() or None
    # source_ref is the TEXTUAL fallback, for material with no live URL — a
    # printed services guide, a PDF page reference. Attribution does not have
    # to be clickable to be honest, but it does have to be present.
    ref = (chunk.get("source_ref") or chunk.get("source_title") or "").strip() or None
    return {
        "service": chunk.get("service"),
        "authority": chunk.get("authority"),
        "url": url,
        "title": chunk.get("source_title"),
        "ref": ref,
    }


def sources_from(chunks):
    """
    Distinct sources behind an answer, in first-seen (best-match) order.

    Deduplicated so a six-section service is cited once rather than six
    times, while a genuinely two-service answer keeps both citations.
    """
    seen, out = set(), []
    for chunk in chunks or []:
        source = _source_of(chunk)
        key = (source["service"], source["url"], source["ref"])
        if key in seen or not (source["service"] or source["url"] or source["ref"]):
            continue
        seen.add(key)
        out.append(source)
    return out


def metadata_from(chunks):
    """
    The provenance block attached to every answer.

    service_name/authority describe the PRIMARY (best-matching) chunk, which
    is what a heading should say. `sources` carries everything cited.
    """
    sources = sources_from(chunks)
    primary = sources[0] if sources else {}
    return {
        "service_name": primary.get("service"),
        "authority": primary.get("authority"),
        "source_url": primary.get("url"),
        "source_title": primary.get("title"),
        "sources": sources,
    }
