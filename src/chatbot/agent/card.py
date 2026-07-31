"""
card.py — D5's answer schema and the missing-field policy.

This module holds the SHAPE of a Daleel answer and the rule for what
happens when the corpus does not supply a field. It deliberately contains
no prompt and no model call: the schema has to be pinned down and testable
independently of generation, because the rule below is a correctness
property, not a prompting preference.

--- The rule that matters ------------------------------------------------

A field with no supporting section in the retrieved context renders as

    «غير محدد في المصدر»   ("not specified in the source")

It is NEVER null, and it is NEVER filled from the model's own knowledge.

Both alternatives are worse in ways that are easy to miss:

  - null / omitted: the UI silently drops the row, and a citizen reading a
    checklist with no «الرسوم» line reasonably concludes the service is
    free. An absent field reads as an answer.

  - filled from model knowledge: produces an invented fee or an invented
    document list sitting underneath a real government source URL. The
    citation makes the fabrication look verified. This is the single worst
    output this system can produce, and it is what whole-service loading
    plus this policy exist to prevent.

Saying "the source does not specify this" is a true statement, and it tells
the citizen exactly what to go and check.
"""

from dataclasses import dataclass

# The literal a missing field renders as. One constant, so the UI, the
# prompt and the tests cannot drift apart.
NOT_IN_SOURCE = "غير محدد في المصدر"

# Confidence, per L3. The model reports this about its OWN grounding:
#   high    — the context answers the question
#   partial — the context answers some of it
#   none    — the context does not contain the answer; do not invent one
CONFIDENCE_VALUES = ("high", "partial", "none")


@dataclass(frozen=True)
class CardField:
    key: str          # JSON key the model emits
    label_ar: str     # what the UI shows
    section_ar: str   # the '##' heading in the corpus that supplies it
    is_list: bool = False


# Field order is reading order: who it is for, what to bring, what to do,
# what it costs, how long, where. The corpus '##' headings in
# docs/CORPUS_SPEC.md are these `section_ar` values, which is why using
# different headings degrades the card.
CARD_FIELDS = (
    CardField("eligibility",        "الفئة المستهدفة",   "الفئة المستهدفة"),
    CardField("required_documents", "المستندات المطلوبة", "المستندات المطلوبة", is_list=True),
    CardField("steps",              "الخطوات",           "الخطوات", is_list=True),
    CardField("fees",               "الرسوم",            "الرسوم"),
    CardField("estimated_time",     "المدة المتوقعة",    "المدة المتوقعة"),
    CardField("where",              "أين تتم المعاملة",  "أين تتم المعاملة"),
)

# Filled from chunk metadata, not from the model. These are facts about
# provenance, and the model is not the authority on where its own context
# came from.
METADATA_KEYS = ("service_name", "authority", "source_url", "source_title")


def sections_present(chunks):
    """The set of '##' headings actually available in this context."""
    return {c.get("section") for c in chunks if c.get("section")}


def missing_fields(chunks):
    """
    Which card fields have no supporting section in the retrieved context.

    Computed from the CONTEXT, not from the model's output — so it is known
    before generation runs and can be asserted against afterwards.
    """
    present = sections_present(chunks)
    return [f.key for f in CARD_FIELDS if f.section_ar not in present]


def metadata_from(chunks):
    """
    Provenance, taken from the corpus rather than from the model.

    source_url in particular must never be model-generated: a hallucinated
    citation is worse than no citation, because it survives a glance.
    """
    if not chunks:
        return {k: None for k in METADATA_KEYS}
    first = chunks[0]
    return {
        "service_name": first.get("service"),
        "authority": first.get("authority"),
        "source_url": first.get("source_url"),
        "source_title": first.get("source_title"),
    }


def apply_missing_field_policy(card, chunks):
    """
    Force every field the context could not support to NOT_IN_SOURCE.

    Applied AFTER generation, deliberately. The prompt also instructs the
    model to do this, but instructions are not guarantees — a model asked
    for eight fields will tend to produce eight plausible fields. This makes
    the policy structural: a field whose section is absent from the context
    is overwritten regardless of what the model returned, so an invented fee
    cannot survive even if the model ignores the instruction.
    """
    result = dict(card)
    absent = set(missing_fields(chunks))

    for f in CARD_FIELDS:
        value = result.get(f.key)
        if f.key in absent or value in (None, "", [], {}):
            result[f.key] = NOT_IN_SOURCE
        elif f.is_list and isinstance(value, list):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            result[f.key] = cleaned or NOT_IN_SOURCE

    result.update(metadata_from(chunks))
    if result.get("confidence") not in CONFIDENCE_VALUES:
        # An unparseable or absent confidence is not evidence of grounding.
        result["confidence"] = "none"
    return result


def empty_card(confidence="none"):
    """A fully-unsupported card. Used when there is no context at all."""
    card = {f.key: NOT_IN_SOURCE for f in CARD_FIELDS}
    card.update({k: None for k in METADATA_KEYS})
    card["confidence"] = confidence
    return card
