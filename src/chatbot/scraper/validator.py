"""
validator.py — the extraction validator.

Pure string matching. No LLM, no embeddings, no judgement. Its whole
purpose is to be a check the LLM cannot talk its way past: if a value in
the generated markdown does not literally appear in the raw page text, the
file is rejected.

--- Why this is the load-bearing part ------------------------------------

The scraper asks a language model to restructure a government page. The
model is instructed not to invent. That instruction is not a guarantee —
it is the same class of assurance as D5's prompt, and D5 needed a
structural post-pass behind it for exactly this reason.

An invented fee inside a corpus file is worse than an invented fee at
generation time. Generation output is transient; a corpus file is
persistent, gets embedded, gets cited with a real government URL, and is
believed by every later answer. So the check that stands between a model
and the corpus has to be mechanical.

--- What it checks -------------------------------------------------------

  1. every NUMBER in the body appears in the raw text
  2. every line of «المستندات المطلوبة» appears in the raw text
  3. no '##' section is empty or implausibly short
  4. the service name appears in the raw text

Both sides are normalized with the project's D3 normalize_arabic() before
comparison, which also folds Arabic-Indic digits — so a page written «٥»
and a file written «5» compare equal rather than producing a false alarm.

--- What it deliberately does NOT check ----------------------------------

Meaning. A validator that tried to judge whether the restructuring was
faithful would need a model, and would inherit the problem it exists to
solve. This catches fabrication and omission of literal values; a human
still confirms the content says what the page says. That is why
verified_by stays PENDING.
"""

import re

from chatbot.rag.arabic import normalize_arabic

# A '##' section shorter than this is either an empty heading the model
# emitted out of habit, or a truncated generation. Both mean the file is
# not finished.
MIN_SECTION_CHARS = 10

# Ordered-list markers are FORMATTING, not content: the "1." in
# "1. تقديم الطلب" was written by the structuring step, not copied from the
# page, so counting it as a number would fail every file with steps.
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_TABLE_RULE = re.compile(r"^\s*\|[\s|:-]+\|\s*$", re.MULTILINE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


class Finding:
    """One failed check, in enough detail to fix without re-reading the page."""

    def __init__(self, check, detail):
        self.check = check
        self.detail = detail

    def __str__(self):
        return f"[{self.check}] {self.detail}"


def _canon(text):
    """
    Normalize for comparison: D3 folding (which converts Arabic-Indic
    digits to ASCII), then strip punctuation and collapse whitespace.

    Punctuation goes because the model reliably reformats it — a page
    bullet «- البطاقة الشخصية.» becomes «البطاقة الشخصية» in the file, and
    that is a formatting difference, not a fabrication.
    """
    return _PUNCT.sub(" ", normalize_arabic(text or "")).replace("  ", " ").strip()


def _body_without_formatting(body):
    """Body text with generated list markers and table rules removed."""
    body = _LIST_MARKER.sub("", body)
    return _TABLE_RULE.sub("", body)


def split_sections(body):
    """[(heading, text)] for every '##' in the generated markdown."""
    parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE)
    out = []
    for i in range(1, len(parts), 2):
        out.append((parts[i].strip(), parts[i + 1].strip() if i + 1 < len(parts) else ""))
    return out


def validate(body, raw_text, service_name):
    """
    Return a list of Finding. Empty list means the file passed.

    `body` is the generated markdown WITHOUT front-matter — front-matter
    carries the retrieval date and other machine-set values that are not
    expected to appear on the page.
    """
    findings = []
    raw = _canon(raw_text)
    raw_digits = set(_NUMBER.findall(raw))

    # --- 4. service name --------------------------------------------------
    if service_name and _canon(service_name) not in raw:
        findings.append(Finding(
            "service_name_not_in_source",
            f"service name {service_name!r} does not appear in the page text"))

    sections = split_sections(body)
    if not sections:
        findings.append(Finding("no_sections", "the generated file has no '##' sections"))
        return findings

    # --- 3. empty or stub sections ---------------------------------------
    for heading, text in sections:
        if len(text.strip()) < MIN_SECTION_CHARS:
            findings.append(Finding(
                "section_too_short",
                f"section «{heading}» has {len(text.strip())} chars "
                f"(minimum {MIN_SECTION_CHARS}) — omit the section instead of "
                f"emitting it empty"))

    # --- 1. numbers -------------------------------------------------------
    # Every figure in the file must be traceable to the page. This is the
    # check that catches an invented fee.
    clean_body = _canon(_body_without_formatting(body))
    for number in _NUMBER.findall(clean_body):
        if number in raw_digits:
            continue
        # A decimal written 0.600 on the page may be copied as 0.6, and
        # vice versa. Accept a trailing-zero difference, nothing looser.
        if any(number.rstrip("0").rstrip(".") == r.rstrip("0").rstrip(".")
               for r in raw_digits if "." in number or "." in r):
            continue
        findings.append(Finding(
            "number_not_in_source",
            f"the number {number!r} appears in the generated file but NOT in "
            f"the page text — possible fabrication"))

    # --- 2. required documents -------------------------------------------
    # Documents are what a citizen physically carries to a counter. An
    # invented one sends them home again.
    for heading, text in sections:
        if "المستندات" not in heading:
            continue
        for line in text.splitlines():
            item = _canon(_LIST_MARKER.sub("", line).lstrip("-*• ").strip())
            if len(item) < 3:
                continue
            if item not in raw:
                findings.append(Finding(
                    "document_not_in_source",
                    f"required document «{line.strip()}» does not appear in the "
                    f"page text"))

    return findings


def summarise(findings):
    """One line per finding, for the .txt note beside a rejected file."""
    if not findings:
        return "PASSED — every check satisfied."
    by_check = {}
    for f in findings:
        by_check.setdefault(f.check, []).append(f.detail)
    lines = [f"FAILED — {len(findings)} finding(s) across "
             f"{len(by_check)} check(s).", ""]
    for check, details in by_check.items():
        lines.append(f"## {check}  ({len(details)})")
        lines.extend(f"  - {d}" for d in details)
        lines.append("")
    return "\n".join(lines)
