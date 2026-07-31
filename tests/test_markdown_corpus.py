"""
Corpus loader tests — the delivery contract in docs/CORPUS_SPEC.md,
enforced.

The corpus is hand-curated by several people under deadline. The value of
these tests is that a malformed file FAILS THE BUILD with a named error
rather than being silently skipped: a quietly-dropped service is one Daleel
will deny having while the file sits right there in the directory.
"""

import pytest

from chatbot.rag.markdown_corpus import (
    CorpusError,
    build_embed_text,
    is_corpus_file,
    load_file,
    parse_front_matter,
    split_sections,
    validate_front_matter,
)

GOOD = """---
authority: شرطة عمان السلطانية
authority_en: Royal Oman Police
service: تجديد جواز السفر
service_id: rop-passport-renewal
source_url: https://www.rop.gov.om/services/passport
source_title: تجديد جواز السفر
retrieved: 2026-07-31
verified_by: إبراهيم
---

# تجديد جواز السفر

## المستندات المطلوبة
- البطاقة الشخصية
- الجواز القديم

## الرسوم
عشرون ريالاً عمانياً.
"""


def write(tmp_path, text, name="rop-passport-renewal.md"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- The happy path ------------------------------------------------------

def test_loads_one_chunk_per_section(tmp_path):
    chunks = load_file(write(tmp_path, GOOD))
    assert len(chunks) == 2
    assert [c["section"] for c in chunks] == ["المستندات المطلوبة", "الرسوم"]


def test_every_chunk_carries_its_source_url(tmp_path):
    """
    D5 cites source_url to the citizen. A chunk that reaches generation
    without one produces an answer Daleel cannot attribute -- which is the
    whole basis of the "how do you know it isn't hallucinating" answer.
    """
    for chunk in load_file(write(tmp_path, GOOD)):
        assert chunk["source_url"].startswith("https://")
        assert chunk["service_id"] == "rop-passport-renewal"
        assert chunk["authority"] == "شرطة عمان السلطانية"


def test_stored_text_stays_clean_but_embed_text_carries_context(tmp_path):
    """
    A section headed «الرسوم» has nothing in its body naming the service,
    so the EMBEDDED text gets the document context prefixed. The STORED
    text stays clean, because that is what reaches the generation prompt
    and what a human proofreads.
    """
    fees = [c for c in load_file(write(tmp_path, GOOD)) if c["section"] == "الرسوم"][0]
    assert "شرطة عمان السلطانية" not in fees["text"]
    assert "شرطة عمان السلطانية" in fees["embed_text"]
    assert "تجديد جواز السفر" in fees["embed_text"]


def test_content_before_the_first_header_is_not_lost(tmp_path):
    text = GOOD.replace("# تجديد جواز السفر\n", "# تجديد جواز السفر\n\nمقدمة مهمة عن الخدمة.\n")
    chunks = load_file(write(tmp_path, text))
    assert any("مقدمة مهمة" in c["text"] for c in chunks)


def test_leading_html_comment_is_allowed(tmp_path):
    """Fixtures and the worked example carry a warning banner up top."""
    chunks = load_file(write(tmp_path, "<!-- FIXTURE -->\n" + GOOD))
    assert len(chunks) == 2


# --- Validation: fail loudly, and say which field ------------------------

@pytest.mark.parametrize("field", [
    "authority", "authority_en", "service", "service_id",
    "source_url", "source_title", "retrieved", "verified_by",
])
def test_every_required_field_is_required(field):
    meta = {f: "x" for f in (
        "authority", "authority_en", "service", "service_id",
        "source_title", "retrieved", "verified_by")}
    meta["source_url"] = "https://example.com"
    del meta[field]
    with pytest.raises(CorpusError) as exc:
        validate_front_matter(meta, "test.md")
    assert field in str(exc.value)


def test_source_url_must_be_a_real_url():
    meta = {f: "x" for f in (
        "authority", "authority_en", "service", "service_id",
        "source_title", "retrieved", "verified_by")}
    meta["source_url"] = "rop.gov.om"          # missing scheme
    with pytest.raises(CorpusError, match="source_url"):
        validate_front_matter(meta, "test.md")


def test_missing_front_matter_is_a_named_error(tmp_path):
    with pytest.raises(CorpusError, match="front-matter"):
        load_file(write(tmp_path, "# عنوان\n\n## قسم\nنص"))


def test_leading_blank_lines_are_tolerated(tmp_path):
    """
    Deliberately lenient. A blank line above the front-matter is a common
    editor artefact with exactly one possible interpretation, so rejecting
    it would produce a build failure that teaches the curation team nothing.
    Strictness is spent where it catches real mistakes -- see the test below.
    """
    assert len(load_file(write(tmp_path, "\n\n" + GOOD))) == 2


def test_content_above_the_front_matter_is_rejected(tmp_path):
    """
    This one IS a real mistake: a heading written above the metadata means
    the author misunderstood the format, and the fields they think they set
    would be silently ignored.
    """
    with pytest.raises(CorpusError, match="front-matter"):
        load_file(write(tmp_path, "# تجديد جواز السفر\n\n" + GOOD))


def test_malformed_yaml_is_a_named_error(tmp_path):
    bad = GOOD.replace("verified_by: إبراهيم", "verified_by: [unclosed")
    with pytest.raises(CorpusError, match="YAML"):
        load_file(write(tmp_path, bad))


def test_file_with_no_sections_is_an_error(tmp_path):
    head = GOOD.split("# تجديد")[0]
    with pytest.raises(CorpusError, match="no content"):
        load_file(write(tmp_path, head))


# --- Documentation living beside the corpus is not corpus ----------------

@pytest.mark.parametrize("name,is_corpus", [
    ("rop-passport-renewal.md", True),
    ("README.md", False),
    ("readme.md", False),
    ("_draft.md", False),
])
def test_skips_documentation_files_by_name(name, is_corpus):
    assert is_corpus_file(name) is is_corpus


# --- Helpers -------------------------------------------------------------

def test_split_sections_handles_multiple_headers():
    body = "# T\n\n## أ\nنص أ\n\n## ب\nنص ب\n\n## ج\nنص ج\n"
    assert [h for h, _ in split_sections(body)] == ["أ", "ب", "ج"]


def test_build_embed_text_shape():
    meta = {"authority": "جهة", "service": "خدمة"}
    out = build_embed_text(meta, "الرسوم", "عشرون ريالاً")
    assert out.startswith("جهة | خدمة | الرسوم\n")


def test_parse_front_matter_returns_body_without_metadata():
    meta, body = parse_front_matter(GOOD, "t.md")
    assert meta["service_id"] == "rop-passport-renewal"
    assert body.lstrip().startswith("# تجديد جواز السفر")
