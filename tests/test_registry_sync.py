"""
Automatic registry registration tests.

A corpus file whose authority is not declared indexes perfectly and is then
unreachable forever: L1 refuses an unknown authority BEFORE retrieval runs,
so the only symptom is a refusal that looks deliberate. This module closes
that gap automatically, which means it can also do damage automatically —
it writes to the file that decides what Daleel claims to cover.

So the tests split in two: that it registers what it should, and that
everything it generates is derived rather than invented.
"""

import json

import pytest

from chatbot.rag.registry_sync import (
    format_report,
    generate_id,
    looks_like_an_authority,
    sync,
    transliterate,
)

FILE = """---
authority: {authority}
authority_en: {authority_en}
service: {service}
service_id: {service_id}
source_url: https://example.gov.om/x
source_title: صفحة الخدمة
retrieved: 2026-08-17
verified_by: PENDING
---

# {service}

## الرسوم

خمسة ريالات.
"""


@pytest.fixture
def corpus(tmp_path):
    def write(service_id, authority, service, authority_en="Ministry of Testing"):
        path = tmp_path / f"{service_id}.md"
        path.write_text(FILE.format(
            authority=authority, authority_en=authority_en,
            service=service, service_id=service_id), encoding="utf-8")
        return path
    return write


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "authorities.json"
    path.write_text(json.dumps({
        "covered": [{
            "id": "rop", "name_ar": "شرطة عمان السلطانية",
            "name_en": "Royal Oman Police",
            "portal_url": "https://www.rop.gov.om",
            "variants": ["شرطة عمان السلطانية", "الشرطة"],
            "services": [{"service_id": "rop-passport-renewal",
                          "name_ar": "تجديد جواز السفر", "variants": []}],
        }],
        "not_covered": [{"name_ar": "وزارة العمل", "name_en": "Ministry of Labour",
                         "variants": ["وزارة العمل"]}],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- Generated ids match the convention already in the file --------------

@pytest.mark.parametrize("name_en,expected", [
    ("Ministry of Health", "moh"),
    ("Ministry of Education", "moe"),
    ("Ministry of Social Development", "mosd"),
    ("Royal Oman Police", "rop"),
    ("Ministry of Commerce, Industry and Investment Promotion", "mociip"),
])
def test_generated_ids_reproduce_the_hand_written_ones(name_en, expected):
    """
    These five ids were written by hand before any of this existed. An
    initialism reproduces them exactly, which is what makes it the
    established convention rather than a second one competing with it.
    """
    assert generate_id(name_en, "", set()) == expected


def test_a_colliding_id_is_suffixed_not_overwritten():
    assert generate_id("Ministry of Health", "", {"moh"}) == "moh2"


def test_an_id_falls_back_to_transliteration_without_english():
    assert generate_id("", "وزارة الصحة", set()).isascii()


# --- The guard against registering OCR debris ----------------------------

@pytest.mark.parametrize("name", [
    "وزارة الصحة", "الهيئة العامة للتأمينات الاجتماعية", "بلدية مسقط",
    "الإدارة العامة للجوازات والأحوال المدنية",
])
def test_real_authority_names_are_accepted(name):
    assert looks_like_an_authority(name) is True


@pytest.mark.parametrize("junk", [
    "ا ال ال ال م",        # OCR debris
    "الرسوم",              # a field label
    "وزارة",               # one word
    "", "   ",
])
def test_debris_cannot_create_a_government_body(junk):
    """
    A registry entry changes what Daleel claims to cover. Creating one from
    a mangled line would put a service under an authority that does not
    exist, with a citation attached.
    """
    assert looks_like_an_authority(junk) is False


# --- Registration --------------------------------------------------------

def test_an_unknown_authority_is_added_to_covered(corpus, registry, tmp_path):
    corpus("mot-01", "وزارة الاختبار", "خدمة تجريبية", "Ministry of Testing")
    report = sync(tmp_path, registry)

    entry = next(a for a in load(registry)["covered"] if a["name_ar"] == "وزارة الاختبار")
    assert entry["id"] == "mot"
    assert [s["service_id"] for s in entry["services"]] == ["mot-01"]
    assert report["added"][0]["id"] == "mot"


def test_covered_is_the_only_destination(corpus, registry, tmp_path):
    """
    Adding to not_covered would leave the file exactly as unreachable as
    before. Whether it SHOULD be answerable is a human call, which is what
    the warning and the auto_registered flag are for.
    """
    corpus("mot-01", "وزارة الاختبار", "خدمة", "Ministry of Testing")
    sync(tmp_path, registry)
    data = load(registry)
    assert "وزارة الاختبار" in {a["name_ar"] for a in data["covered"]}
    assert "وزارة الاختبار" not in {a["name_ar"] for a in data["not_covered"]}


def test_an_auto_added_entry_is_flagged_for_review(corpus, registry, tmp_path):
    """Reverting one requires finding it first."""
    corpus("mot-01", "وزارة الاختبار", "خدمة", "Ministry of Testing")
    sync(tmp_path, registry)
    entry = next(a for a in load(registry)["covered"] if a["id"] == "mot")
    assert entry["auto_registered"] is True


def test_no_portal_url_is_invented(corpus, registry, tmp_path):
    """
    portal_url is shown to citizens in a D6 refusal. A plausible-looking
    guess sends someone to a domain that may not exist.
    """
    corpus("mot-01", "وزارة الاختبار", "خدمة", "Ministry of Testing")
    sync(tmp_path, registry)
    entry = next(a for a in load(registry)["covered"] if a["id"] == "mot")
    assert entry["portal_url"] is None


def test_variants_are_only_what_was_seen(corpus, registry, tmp_path):
    """
    L1 is good because its variants are how people actually speak. None of
    that is derivable from a formal ministry name, so guessing at phrasings
    would be inventing knowledge about citizens.
    """
    corpus("mot-01", "وزارة الاختبار", "خدمة تجريبية", "Ministry of Testing")
    sync(tmp_path, registry)
    entry = next(a for a in load(registry)["covered"] if a["id"] == "mot")
    assert entry["variants"] == ["وزارة الاختبار"]
    assert entry["services"][0]["variants"] == ["خدمة تجريبية"]


def test_debris_authority_is_reported_not_registered(corpus, registry, tmp_path):
    corpus("junk-01", "ا ال ال ال م", "خدمة", "X")
    report = sync(tmp_path, registry)
    assert report["rejected"] == ["ا ال ال ال م"]
    assert len(load(registry)["covered"]) == 1


# --- Existing entries are left alone -------------------------------------

def test_an_existing_authority_is_not_rewritten(corpus, registry, tmp_path):
    corpus("rop-passport-renewal", "شرطة عمان السلطانية", "تجديد جواز السفر",
           "Royal Oman Police")
    before = load(registry)["covered"][0]
    sync(tmp_path, registry)
    after = load(registry)["covered"][0]
    assert after["portal_url"] == before["portal_url"]
    assert after["variants"] == before["variants"]
    assert "auto_registered" not in after


def test_a_new_service_joins_an_existing_authority(corpus, registry, tmp_path):
    """Matched via the authority's declared variants, not just its name."""
    corpus("rop-birth-certificate", "الشرطة", "استخراج شهادة ميلاد",
           "Royal Oman Police")
    sync(tmp_path, registry)
    rop = next(a for a in load(registry)["covered"] if a["id"] == "rop")
    assert "rop-birth-certificate" in {s["service_id"] for s in rop["services"]}
    assert len(load(registry)["covered"]) == 1      # no duplicate authority


def test_service_ids_come_from_the_files(corpus, registry, tmp_path):
    """
    service_id is the join key. Generating a different one here would name
    services that do not exist — the exact drift that made an earlier
    version of this put unreachable ids in the registry.
    """
    corpus("mot-07", "وزارة الاختبار", "خدمة", "Ministry of Testing")
    sync(tmp_path, registry)
    entry = next(a for a in load(registry)["covered"] if a["id"] == "mot")
    assert [s["service_id"] for s in entry["services"]] == ["mot-07"]


def test_running_twice_changes_nothing(corpus, registry, tmp_path):
    corpus("mot-01", "وزارة الاختبار", "خدمة", "Ministry of Testing")
    sync(tmp_path, registry)
    first = registry.read_text(encoding="utf-8")
    second_report = sync(tmp_path, registry)
    assert registry.read_text(encoding="utf-8") == first
    assert second_report["added"] == [] and second_report["services_added"] == []


def test_a_promoted_authority_leaves_not_covered(corpus, registry, tmp_path):
    """
    A name in both lists lets the uncovered check fire on an authority the
    corpus covers — an instant refusal for a question Daleel can answer.
    """
    corpus("mol-01", "وزارة العمل", "تصريح عمل", "Ministry of Labour")
    report = sync(tmp_path, registry)
    data = load(registry)
    assert "وزارة العمل" not in {a["name_ar"] for a in data["not_covered"]}
    assert "وزارة العمل" in {a["name_ar"] for a in data["covered"]}
    assert report["promoted"] == ["وزارة العمل"]


def test_a_dry_run_writes_nothing(corpus, registry, tmp_path):
    corpus("mot-01", "وزارة الاختبار", "خدمة", "Ministry of Testing")
    before = registry.read_text(encoding="utf-8")
    report = sync(tmp_path, registry, write=False)
    assert registry.read_text(encoding="utf-8") == before
    assert report["added"]          # still reports what it would do


def test_an_unreadable_file_does_not_stop_the_rest(corpus, registry, tmp_path):
    """One malformed file must not leave every other authority unroutable."""
    (tmp_path / "broken.md").write_text("no front matter here", encoding="utf-8")
    corpus("mot-01", "وزارة الاختبار", "خدمة", "Ministry of Testing")
    sync(tmp_path, registry)
    assert any(a["id"] == "mot" for a in load(registry)["covered"])


# --- One service, one owner ----------------------------------------------
# Checking only the authority's own list registered rop-passport-issuance
# twice: once under شرطة عمان السلطانية where it had always been, and again
# under الإدارة العامة للجوازات, because the corpus file names the directorate
# printed on the page rather than the police.

def test_a_service_owned_elsewhere_is_not_duplicated(corpus, registry, tmp_path):
    corpus("rop-passport-renewal", "الإدارة العامة للجوازات والأحوال المدنية",
           "تجديد جواز السفر", "Directorate General of Passports")
    report = sync(tmp_path, registry)

    owners = [a["id"] for a in load(registry)["covered"]
              for s in a["services"] if s["service_id"] == "rop-passport-renewal"]
    assert owners == ["rop"], "one service_id must have exactly one owner"
    assert report["conflicts"][0]["service_id"] == "rop-passport-renewal"
    assert report["conflicts"][0]["registered_under"] == "rop"


def test_an_authority_whose_services_all_belong_elsewhere_is_not_added(
        corpus, registry, tmp_path):
    """
    Declaring an authority with no services behind it is the inverse of the
    bug this module exists to fix: coverage claimed, nothing to answer with.
    """
    corpus("rop-passport-renewal", "الإدارة العامة للجوازات والأحوال المدنية",
           "تجديد جواز السفر", "Directorate General of Passports")
    report = sync(tmp_path, registry)
    assert [a["id"] for a in load(registry)["covered"]] == ["rop"]
    assert report["added"] == []
    assert report["dropped_empty"] == ["dgop"]


def test_a_conflict_is_reported_loudly(corpus, registry, tmp_path):
    corpus("rop-passport-renewal", "الإدارة العامة للجوازات والأحوال المدنية",
           "تجديد جواز السفر", "Directorate General of Passports")
    text = format_report(sync(tmp_path, registry))
    assert "CONFLICT" in text
    assert "rop-passport-renewal" in text


def test_naming_a_directorate_as_a_variant_resolves_the_conflict(
        corpus, registry, tmp_path):
    """
    The fix a human applies after reading that warning: the directorate is
    ROP's passports department, so its wording becomes a variant and both
    names route to one authority.
    """
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["covered"][0]["variants"].append("الإدارة العامة للجوازات والأحوال المدنية")
    registry.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    corpus("rop-birth-certificate", "الإدارة العامة للجوازات والأحوال المدنية",
           "استخراج شهادة ميلاد", "Directorate General of Passports")
    report = sync(tmp_path, registry)

    assert report["conflicts"] == []
    assert len(load(registry)["covered"]) == 1
    rop = load(registry)["covered"][0]
    assert "rop-birth-certificate" in {s["service_id"] for s in rop["services"]}


# --- The warning is the point --------------------------------------------

def test_the_report_names_every_consequence(corpus, registry, tmp_path):
    """
    Silent auto-registration would be worse than the bug it fixes: Daleel
    would quietly start claiming to cover things nobody approved.
    """
    corpus("mot-01", "وزارة الاختبار", "خدمة", "Ministry of Testing")
    text = format_report(sync(tmp_path, registry))
    assert "AUTO-REGISTERED" in text
    assert "وزارة الاختبار" in text
    assert "VARIANTS ARE MINIMAL" in text
    assert "portal_url" in text
    assert "auto_registered" in text


def test_a_transliterated_english_name_is_called_out(corpus, registry, tmp_path):
    """
    An Arabic PDF has no English name, so one is transliterated. That is a
    placeholder, and shipping it unnoticed would put «Wzarh Alakhtbar» in
    front of a judge.
    """
    corpus("mot-01", "وزارة الاختبار", "خدمة", authority_en="")
    text = format_report(sync(tmp_path, registry))
    assert "transliterated" in text


def test_transliteration_is_ascii_only():
    assert transliterate("وزارة الصحة").isascii()
