"""
registry_sync.py — make every corpus file routable, mechanically.

The corpus and data/registry/authorities.json are joined by two strings:
the authority name and the service_id. When a corpus file names an
authority the registry has never heard of, the file indexes perfectly and
is then unreachable forever — L1 refuses an unknown authority BEFORE
retrieval runs, so no question can ever reach it. build_index.py already
detects that state and prints UNROUTABLE; this module removes the need to
fix it by hand.

--- Driven by the corpus, not by the importer ----------------------------

Registration reads data/source_docs/*.md rather than the importer's
in-memory results. Those files are what actually exists, so:

  - a PDF import registers because it wrote files;
  - a hand-written markdown file dropped into the corpus registers too;
  - re-running is idempotent, because it compares against what is there.

An earlier version derived this from the importer's own accepted-list. It
drifted the same week: the importer changed how it numbered service_ids and
the registration code did not, so the registry named services that did not
exist and every one of them refused while sitting fully indexed.

--- Mechanical, not smart ------------------------------------------------

Everything generated here is derivable from the file:

  id           an initialism of authority_en — "Ministry of Social
               Development" -> mosd. This reproduces 5 of the 6 existing
               hand-written ids exactly, so it is genuinely the same style
               rather than a new one.
  name_ar      as written in the file.
  name_en      as written in the file; transliterated from Arabic only if
               the file has none.
  portal_url   None. It appears in D6 refusals and is shown to citizens, so
               a guessed domain is worse than an absent one.
  variants     the literal name, and nothing else.

That last one is the important limitation. L1 is good because its variants
are how people actually speak — «رخصتي منتهية», «بطاقتي», «جواز لاول مرة».
None of that is derivable from a formal ministry name, and inventing
phrasings would be guessing at how citizens talk. So an auto-registered
authority matches only its own formal name until a human adds the rest,
and `report()` says so loudly.
"""

import json
import logging
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

# Words that carry no distinguishing information in an English authority
# name. "of" is KEPT because the existing ids keep it: moh, moe, mosd.
_SKIP_WORDS = {"and", "the", "for", "&"}

# An authority heading has to look like one. OCR debris and stray prose
# must not be able to create a government body in the registry.
#
# «الإدارة» earns its place from a real file: the scraper recorded
# rop-passport-issuance under «الإدارة العامة للجوازات والأحوال المدنية»,
# the directorate named on the page, rather than under شرطة عمان السلطانية.
# That file has been unroutable ever since.
_AUTHORITY_PREFIXES = (
    "وزارة", "الوزارة", "هيئة", "الهيئة", "بلدية", "البلدية", "شرطة",
    "الشرطة", "ديوان", "الديوان", "مكتب", "المكتب", "جهاز", "الجهاز",
    "مركز", "المركز", "الأمانة", "أمانة", "محافظة", "المحافظة",
    "صندوق", "الصندوق", "إدارة", "الإدارة", "دائرة", "الدائرة",
    "سلطة", "السلطة", "مؤسسة", "المؤسسة", "بنك", "البنك",
)

# Deliberately plain: this only ever produces a fallback slug or a
# placeholder English name, both of which are flagged for a human pass.
_TRANSLITERATION = {
    "ء": "", "آ": "a", "أ": "a", "إ": "a", "ؤ": "w", "ئ": "y", "ا": "a",
    "ب": "b", "ة": "h", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh",
    "د": "d", "ذ": "dh", "ر": "r", "ز": "z", "س": "s", "ش": "sh", "ص": "s",
    "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "q",
    "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h", "و": "w", "ى": "a",
    "ي": "y",
}


class RegistrySyncError(Exception):
    """The registry could not be updated. Never fail silently."""


def normalise(text):
    """Fold an Arabic name for MATCHING only. Never used on stored text."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[ً-ْـ‎‏]", "", text)
    text = re.sub(r"[أإآٱ]", "ا", text).replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", text).strip().lower()


def transliterate(arabic):
    """Arabic letters to bare Latin. Lossy by design, and only a fallback."""
    folded = re.sub(r"[ً-ْـ]", "", unicodedata.normalize("NFKC", arabic or ""))
    out = []
    for char in folded:
        if char in _TRANSLITERATION:
            out.append(_TRANSLITERATION[char])
        elif char.isspace():
            out.append(" ")
        elif char.isascii() and char.isalnum():
            out.append(char.lower())
    return re.sub(r"\s+", " ", "".join(out)).strip()


def looks_like_an_authority(name):
    """
    Guard against registering OCR debris as a government body.

    A registry entry changes what Daleel claims to cover, so the bar to
    create one automatically is that the name reads like an authority: a
    recognised kind of body, at least two words, and predominantly Arabic.
    """
    cleaned = (name or "").strip()
    if len(cleaned.split()) < 2:
        return False
    if not cleaned.startswith(_AUTHORITY_PREFIXES):
        return False
    letters = [c for c in cleaned if c.isalpha()]
    if not letters:
        return False
    arabic = sum(1 for c in letters if "؀" <= c <= "ۿ")
    return arabic / len(letters) >= 0.8


def generate_id(name_en, name_ar, taken):
    """
    A short lowercase id, in the style the hand-written ones already use.

    An initialism of the English name: "Ministry of Social Development" ->
    mosd, "Royal Oman Police" -> rop. Checked against the existing registry
    when this was written, it reproduces moh, moe, mosd, mociip and rop
    exactly — so it is the established convention, not a second one.

    Falls back to transliterated Arabic when a file carries no usable
    English name, and to a numbered slug when even that yields nothing.
    """
    words = [w for w in re.split(r"[\s,\-]+", (name_en or "").strip())
             if w and w.lower() not in _SKIP_WORDS]
    letters = [w[0].lower() for w in words if w[0].isascii() and w[0].isalpha()]
    candidate = "".join(letters)

    if len(candidate) < 2:
        candidate = re.sub(r"[^a-z]", "", transliterate(name_ar).replace(" ", ""))[:10]
    if len(candidate) < 2:
        candidate = "auth"

    unique, suffix = candidate, 1
    while unique in taken:
        suffix += 1
        unique = f"{candidate}{suffix}"
    return unique


def read_corpus_authorities(corpus_dir):
    """
    Group corpus files by the authority they name.

    Reads front-matter directly rather than via load_corpus() so that a file
    which fails validation for some unrelated reason cannot prevent an
    otherwise-healthy authority from being registered.
    """
    from chatbot.rag.markdown_corpus import is_corpus_file, parse_front_matter

    grouped = {}
    for path in sorted(Path(corpus_dir).glob("*.md")):
        if not is_corpus_file(path):
            continue
        try:
            meta, _ = parse_front_matter(path.read_text(encoding="utf-8"), path.name)
        except Exception as exc:                      # noqa: BLE001
            logger.warning("registry_sync_unreadable_file",
                           extra={"file": path.name, "error": str(exc)})
            continue

        authority = str(meta.get("authority", "")).strip()
        service_id = str(meta.get("service_id", "")).strip()
        if not authority or not service_id:
            continue

        entry = grouped.setdefault(normalise(authority), {
            "name_ar": authority,
            "name_en": str(meta.get("authority_en", "")).strip(),
            "services": [],
        })
        entry["services"].append({
            "service_id": service_id,
            "name_ar": str(meta.get("service", "")).strip() or service_id,
        })
    return grouped


def _existing_index(registry):
    """Every name and variant already declared, folded, to its entry."""
    index = {}
    for entry in registry["covered"]:
        for name in [entry.get("name_ar", "")] + list(entry.get("variants", [])):
            folded = normalise(name)
            if folded:
                index.setdefault(folded, entry)
    return index


def sync(corpus_dir, registry_path, write=True):
    """
    Register every authority and service the corpus contains.

    Returns a report. Existing entries are never rewritten — authorities
    gain missing services, and nothing else about them is touched.
    """
    registry_path = Path(registry_path)
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as exc:                          # noqa: BLE001
        raise RegistrySyncError(f"cannot read {registry_path}: {exc}") from exc

    for key in ("covered", "not_covered"):
        if key not in registry:
            raise RegistrySyncError(f"{registry_path}: missing '{key}' list")

    corpus = read_corpus_authorities(corpus_dir)
    index = _existing_index(registry)
    taken = {entry["id"] for entry in registry["covered"]}

    report = {"added": [], "promoted": [], "services_added": [],
              "rejected": [], "conflicts": [], "registry_path": str(registry_path)}

    # service_id is the join key and must be unique across the WHOLE registry,
    # not just within one authority.
    #
    # Checking only the authority's own list registered rop-passport-issuance
    # twice — once under شرطة عمان السلطانية where it had always been, and
    # again under الإدارة العامة للجوازات, because the corpus file names the
    # directorate on the page rather than the police. Two owners for one id is
    # a contradiction about who provides the service.
    owner_of = {s["service_id"]: entry["id"]
                for entry in registry["covered"] for s in entry["services"]}

    for folded, found in corpus.items():
        entry = index.get(folded)

        if entry is None:
            if not looks_like_an_authority(found["name_ar"]):
                report["rejected"].append(found["name_ar"])
                continue

            auth_id = generate_id(found["name_en"], found["name_ar"], taken)
            taken.add(auth_id)
            name_en = found["name_en"] or transliterate(found["name_ar"]).title()
            entry = {
                "id": auth_id,
                "name_ar": found["name_ar"],
                "name_en": name_en,
                # Never invented. This string is shown to citizens in a D6
                # refusal, and a guessed domain is worse than none.
                "portal_url": None,
                # Only what was actually seen. See the module docstring.
                "variants": [found["name_ar"]],
                "services": [],
                "auto_registered": True,
            }
            registry["covered"].append(entry)
            index[folded] = entry
            report["added"].append({
                "id": auth_id, "name_ar": found["name_ar"], "name_en": name_en,
                "derived_name_en": not found["name_en"],
                "services": len(found["services"]),
            })

        for service in found["services"]:
            service_id = service["service_id"]
            owner = owner_of.get(service_id)
            if owner == entry["id"]:
                continue                       # already registered here
            if owner is not None:
                # Registered under a DIFFERENT authority. Reassigning it would
                # rewrite an existing entry, and silently duplicating it would
                # give one service two owners. Report and let a human decide.
                report["conflicts"].append({
                    "service_id": service_id,
                    "registered_under": owner,
                    "corpus_says": entry["id"],
                    "authority_ar": found["name_ar"],
                })
                continue
            entry["services"].append({
                "service_id": service_id,
                "name_ar": service["name_ar"],
                "variants": [service["name_ar"]],
            })
            owner_of[service_id] = entry["id"]
            report["services_added"].append(service_id)

    # An authority auto-added whose every service turned out to belong to
    # someone else has nothing behind it. Leaving it would declare coverage
    # the corpus cannot supply — the inverse of the bug this module fixes.
    empty = {item["id"] for item in report["added"]
             if not any(e["id"] == item["id"] and e["services"]
                        for e in registry["covered"])}
    if empty:
        registry["covered"] = [e for e in registry["covered"]
                               if e["id"] not in empty]
        report["added"] = [i for i in report["added"] if i["id"] not in empty]
        report["dropped_empty"] = sorted(empty)

    # An authority present in both lists would let the uncovered check fire
    # on a name the corpus now covers: an instant refusal for a question
    # Daleel can answer, invisible unless something asserts it.
    covered_names = {normalise(e["name_ar"]) for e in registry["covered"]}
    remaining = []
    for entry in registry["not_covered"]:
        if normalise(entry.get("name_ar", "")) in covered_names:
            report["promoted"].append(entry.get("name_ar"))
        else:
            remaining.append(entry)
    registry["not_covered"] = remaining

    if write and (report["added"] or report["services_added"] or report["promoted"]):
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    report["authorities"] = len(registry["covered"])
    report["services"] = sum(len(a["services"]) for a in registry["covered"])
    return report


def format_report(report, wrote=True):
    """
    The loud half. Auto-registration changes what Daleel claims to cover,
    and it must never be something a person discovers later by accident.
    """
    lines = []
    if report["services_added"]:
        lines.append(f"registry: +{len(report['services_added'])} services")
    if report["promoted"]:
        lines.append("")
        lines.append("  PROMOTED — these were declared not_covered and now have")
        lines.append("  content, so they were moved to covered:")
        lines += [f"    - {name}" for name in report["promoted"]]

    if report["added"]:
        lines.append("")
        lines.append("  " + "!" * 66)
        lines.append("  AUTO-REGISTERED AUTHORITIES — NEEDS A HUMAN PASS")
        lines.append("  " + "!" * 66)
        for item in report["added"]:
            lines.append(f"    {item['id']:<10} {item['name_ar']}  "
                         f"({item['services']} services)")
            if item["derived_name_en"]:
                lines.append(f"    {'':<10} name_en was transliterated: "
                             f"{item['name_en']!r} — replace it")
        lines.append("")
        lines.append("  These were added to `covered` automatically, which is the")
        lines.append("  only way their services become answerable at all. Two")
        lines.append("  consequences you have to review:")
        lines.append("")
        lines.append("  1. VARIANTS ARE MINIMAL. Each matches only its own formal")
        lines.append("     name. A citizen writing «بطاقتي» or «رخصتي منتهية» will")
        lines.append("     NOT reach it. Add real phrasings to")
        lines.append(f"     {report['registry_path']}")
        lines.append("  2. portal_url IS EMPTY, because none was recoverable and a")
        lines.append("     guessed domain is shown to citizens in refusals.")
        lines.append("")
        lines.append("  If an authority should NOT be answerable yet, delete its")
        lines.append("  entry from `covered` — every auto-added entry is marked")
        lines.append('   "auto_registered": true so they are easy to find.')
        lines.append("  " + "!" * 66)

    if report.get("conflicts"):
        lines.append("")
        lines.append("  CONFLICT — these services are already registered under a")
        lines.append("  DIFFERENT authority. One service cannot have two owners, so")
        lines.append("  nothing was changed. Either correct the `authority:` in the")
        lines.append("  corpus file, or add its wording as a variant of the existing")
        lines.append("  authority so both names route to one place:")
        for c in report["conflicts"]:
            lines.append(f"    - {c['service_id']}")
            lines.append(f"        registered under : {c['registered_under']}")
            lines.append(f"        corpus file says : {c['authority_ar']}")

    if report.get("dropped_empty"):
        lines.append("")
        lines.append("  Not added after all (every service belonged elsewhere): "
                     + ", ".join(report["dropped_empty"]))

    if report["rejected"]:
        lines.append("")
        lines.append("  NOT REGISTERED — these do not read as authority names, so")
        lines.append("  their files stay unroutable (likely OCR damage):")
        lines += [f"    - {name!r}" for name in report["rejected"]]

    if not wrote and (report["added"] or report["services_added"]):
        lines.append("")
        lines.append("  (dry run — nothing was written)")

    lines.append("")
    lines.append(f"registry now: {report['authorities']} authorities, "
                 f"{report['services']} services")
    return "\n".join(lines)
