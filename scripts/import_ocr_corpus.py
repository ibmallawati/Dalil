"""
import_ocr_corpus.py — turn OCR text into corpus files, mechanically.

    python scripts/import_ocr_corpus.py                  # inventory only
    python scripts/import_ocr_corpus.py --write          # write the .md files

Reads data/pdf_ocr/*.ocr.txt (produced by scripts/ocr_inbox.py) and emits
data/source_docs/<service_id>.md. Then run scripts/build_index.py.

--- No model. Nothing to fabricate. --------------------------------------

The importer this replaces asked an LLM to restructure OCR text into the
corpus schema. It quietly repaired damage instead of reporting it:

    OCR source:  «.تختلة 1 نوع النشاط والتر خب»    <- garbled
    model wrote: «تختلف حسب نوع النشاط والترخيص»    <- plausible invention

Five of fifteen services failed validation that way, and in one case the
source's documents field was EMPTY and the model supplied one anyway. For
government fees and required documents, that is the failure that sends a
citizen to a counter without the right paper.

So there is no model here. Every transformation below is a pure string
operation: splitting on headings the document already contains, and
character-level cleanups that cannot change meaning. Because nothing is
rewritten, there is nothing to validate — the old validator is retired
rather than reimplemented.

The cost is honest: OCR errors reach the corpus. «سلطنة غُمان» stays
misspelled. A visible typo is a far better failure than an invented fee.

--- Structure the documents already have ---------------------------------

    اسم الجهة: وزارة العمل            <- authority (two spellings, see below)
    الخدمة رقم (3): إصدار تصاريح العمل  <- service, numbering restarts per authority
    :المستندات المطلوبة                <- field label, colon on EITHER side
    ===== PAGE 42 =====               <- page marker, used to build citations

See docs/PDF_INTAKE_SPEC.md for the measurements behind every rule here.
"""

import argparse
import io
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

from chatbot.config import DATA_DIR


def _force_utf8_stdout():
    """
    Windows consoles default to cp1252 and cannot print Arabic.

    Done inside main() rather than at import: rebinding sys.stdout on import
    breaks pytest's output capture, and these functions are worth testing
    directly.
    """
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace")

OCR_DIR = DATA_DIR / "pdf_ocr"
OUT_DIR = DATA_DIR / "source_docs"
REVIEW_DIR = DATA_DIR / "needs_review"
REGISTRY = DATA_DIR / "registry" / "authorities.json"

MIN_FIELDS = 3


# --- Authority identity ---------------------------------------------------
# Folded name -> (id, correct Arabic name, English name).
#
# The KEY is folded (ة->ه, أ->ا) purely for matching, because the OCR spells
# the country three different ways («عُمان», «غُمان», «غمان») and separates
# the ministry from it with either «-» or an RLM.
#
# The Arabic name is written out properly here rather than derived from the
# key, and it is what reaches the front matter. Unfolding a key mechanically
# gets «وزاره العمل» wrong, and this string is displayed to citizens.
#
# Taking a field from a canonical table is not the same as editing OCR text:
# the authority's name is identity, not content.
AUTHORITIES = {
    "وزاره الدفاع": ("mod", "وزارة الدفاع", "Ministry of Defence"),
    "وزاره الداخليه": ("moi", "وزارة الداخلية", "Ministry of Interior"),
    "وزاره الخارجيه": ("mofa", "وزارة الخارجية", "Ministry of Foreign Affairs"),
    "وزاره الماليه": ("mof", "وزارة المالية", "Ministry of Finance"),
    "وزاره التنميه الاجتماعيه": (
        "mosd", "وزارة التنمية الاجتماعية", "Ministry of Social Development"),
    "وزاره التجاره والصناعه وترويج الاستثمار": (
        "mociip", "وزارة التجارة والصناعة وترويج الاستثمار",
        "Ministry of Commerce, Industry and Investment Promotion"),
    "وزاره الطاقه والمعادن": (
        "mem", "وزارة الطاقة والمعادن", "Ministry of Energy and Minerals"),
    "وزاره التراث والسياحه": (
        "mht", "وزارة التراث والسياحة", "Ministry of Heritage and Tourism"),
    "وزاره الاعلام": ("mi", "وزارة الإعلام", "Ministry of Information"),
    "وزاره الاوقاف والشؤون الدينيه": (
        "mara", "وزارة الأوقاف والشؤون الدينية",
        "Ministry of Endowments and Religious Affairs"),
    "وزاره التربيه والتعليم": (
        "moe", "وزارة التربية والتعليم", "Ministry of Education"),
    "وزاره العمل": ("mol", "وزارة العمل", "Ministry of Labour"),
    "وزاره النقل والاتصالات وتقنيه المعلومات": (
        "mtcit", "وزارة النقل والاتصالات وتقنية المعلومات",
        "Ministry of Transport, Communications and Information Technology"),
    "وزاره الثروه الزراعيه والسمكيه وموارد المياه": (
        "mafwr", "وزارة الثروة الزراعية والسمكية وموارد المياه",
        "Ministry of Agricultural Wealth, Fisheries and Water Resources"),
    "وزاره الاسكان والتخطيط العمراني": (
        "mohup", "وزارة الإسكان والتخطيط العمراني",
        "Ministry of Housing and Urban Planning"),
    "وزاره الاقتصاد": ("moec", "وزارة الاقتصاد", "Ministry of Economy"),
    "وزاره الثقافه والرياضه والشباب": (
        "mcsy", "وزارة الثقافة والرياضة والشباب",
        "Ministry of Culture, Sports and Youth"),
    "وزاره العدل والشؤون القانونيه": (
        "moj", "وزارة العدل والشؤون القانونية",
        "Ministry of Justice and Legal Affairs"),
}


# --- Field labels ---------------------------------------------------------
# OCR spelling -> the '##' section heading used by the rest of the corpus.
# `رابط الخدمة` is deliberately absent: every URL in these documents is
# destroyed beyond recovery (0 of 97 recoverable), so importing the field
# would attach unreadable glyphs to an answer.
FIELDS = {
    "وصف الخدمة": "وصف الخدمة",
    "الفئة المستفيدة": "الفئة المستهدفة",
    "الفئة المستهدفة": "الفئة المستهدفة",
    "خطوات الحصول على الخدمة": "الخطوات",
    ".خطوات الحصول على الخدمة": "الخطوات",
    "الخطوات": "الخطوات",
    "المستندات المطلوبة": "المستندات المطلوبة",
    "المستندات المطلوبة عادة": "المستندات المطلوبة",
    "الشروط والمتطلبات": "الشروط والمتطلبات",
    "الشروط": "الشروط والمتطلبات",
    "الرسوم": "الرسوم",
    "مدة الإنجاز": "المدة المتوقعة",
    "مدة إنجاز الخدمة": "المدة المتوقعة",
}

# Emission order. A citizen reads "who is this for" before "what does it
# cost", and retrieval quality does not depend on order, so follow the
# reading order the curated files already use.
SECTION_ORDER = [
    "وصف الخدمة", "الفئة المستهدفة", "الشروط والمتطلبات",
    "المستندات المطلوبة", "الخطوات", "الرسوم", "المدة المتوقعة",
]

# Phrases meaning "this field is blank". A section containing only these is
# not content — importing it produces an answer that reads as "this service
# is free and needs no documents", which is a claim, not an absence.
EMPTY_MARKERS = ["لا توجد", "لا يوجد", "غير محددة", "غير محدد", "لم يتم",
                 "لا تتوفر", "غير منشور", "غير معلن", "لا تنطبق"]

# Not citizen transactions. Excluded by name however many fields they fill:
# a vague question about a ministry that lands on its contact form returns a
# web form instead of a procedure.
EXCLUDE = [
    "تواصل", "نادي الشفق", "البث المباشر", "تحميل الإصدارات",
    "الشكاوى والبلاغات", "البيانات المفتوحة",
    "منصة حزم", "إنجاز", "نظام خدمات المستفيدين",
    "أوامر الشراء", "الاحتياجات المالية للشركات الحكومية", "إصدار الضمانات",
    "القروض التنموية", "شهادات ضريبية لشركات الامتياز",
    "الإيراد والصرف المالي",
]


# --- Patterns -------------------------------------------------------------
# NOT anchored with ^. The probe lost one authority and one service to
# anchoring: these documents put an RLM, a bullet, or a list number before a
# heading often enough that requiring line-start silently drops records.
AUTH_LABELLED = re.compile(r"اسم الجهة\s*:\s*(.+)")
AUTH_NUMBERED = re.compile(r"^\s*\d+\s*\)\s*(وزارة\s+.+)$")
# The closing paren is optional: OCR dropped it on «الخدمة رقم (9: طلب
# الإعفاءات من رسوم العلاج», and requiring it skipped that service.
SERVICE = re.compile(r"الخدمة(?:/القسم)?\s*رقم\s*\(?\s*(\d+)\s*\)?\s*:\s*(.+)")
PAGE = re.compile(r"=====\s*PAGE\s*(\d+)\s*=====")

# Deliberately sloppier than SERVICE, and used ONLY to audit it.
#
# Reconciling the parser against its own regex proves nothing — a heading
# the regex cannot see is invisible to both. This second pattern is written
# to over-match, so anything that looks remotely like a service heading and
# did NOT become a record gets reported. That is how the missing paren above
# was found.
SERVICE_LOOSE = re.compile(r"الخدمة.{0,12}رقم")

BIDI = dict.fromkeys(map(ord, "​‌‍‎‏؜﻿"))
TATWEEL = dict.fromkeys([0x0640])


def normalise_name(text):
    """Fold an Arabic name for MATCHING only. Never used on shipped text."""
    text = unicodedata.normalize("NFKC", text).translate(BIDI).translate(TATWEEL)
    text = re.sub(r"[ً-ْ]", "", text)
    text = re.sub(r"[أإآٱ]", "ا", text).replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", text).strip()


# «وزارة العمل - سلطنة عُمان» / «وزارة الداخلية ‏ سلطنة عُمان». The country
# is not part of the authority's name, and it is where the OCR damage sits
# («غُمان» in 6 of 17 headings).
_COUNTRY_SUFFIX = re.compile(r"\s*[-–—‏]\s*(سلطنة|سلطنه)\s+\S+\s*$")


def match_authority(raw):
    """
    Resolve an OCR'd authority heading to (id, name_ar, name_en).

    A known authority is read from the table above, so the canonical Arabic
    reaches the front matter instead of «سلطنة غُمان».

    An UNKNOWN one is no longer rejected. It used to be, which meant a PDF
    for a ministry nobody had hand-listed here produced nothing at all —
    every one of its services silently dropped for a reason that had nothing
    to do with the service. Now its identity is derived mechanically and it
    is registered on the way out, loudly. See chatbot.rag.registry_sync.

    Returns None only when the heading does not read like an authority at
    all, which is how OCR debris is kept out of the registry.
    """
    folded = normalise_name(raw)
    for key, identity in AUTHORITIES.items():
        if folded.startswith(key):
            return identity

    from chatbot.rag.registry_sync import (generate_id,
                                           looks_like_an_authority,
                                           transliterate)

    name_ar = _COUNTRY_SUFFIX.sub("", (raw or "").strip()).strip()
    if not looks_like_an_authority(name_ar):
        return None

    # No English name exists in an Arabic PDF, so it is transliterated and
    # flagged for replacement rather than guessed at.
    name_en = transliterate(name_ar).title()
    return generate_id("", name_ar, set(AUTHORITIES_IDS)), name_ar, name_en


AUTHORITIES_IDS = {identity[0] for identity in AUTHORITIES.values()}


# --- Cleaning -------------------------------------------------------------
# Every rule is a pure string operation that cannot change meaning. Nothing
# here fixes a misspelled word, completes a sentence, or fills a gap.

def clean_line(line):
    line = unicodedata.normalize("NFKC", line).translate(BIDI).translate(TATWEEL)
    line = line.replace("»", "،").replace("«", "،")
    line = line.strip()

    # RTL OCR emits list markers at the wrong end: «.الدخول إلى الموقع .1»
    trailing_number = re.match(r"^(.*?)\s*\.(\d+)$", line)
    if trailing_number:
        line = f"{trailing_number.group(2)}. {trailing_number.group(1).strip()}"

    # ...and sentence-final periods at the start: «.أداء أدوارها الوطنية»
    if line.startswith(".") and not re.match(r"^\.\d", line):
        line = line[1:].strip() + "."

    # Bullet glyphs. Tesseract renders the document's bullet as a lone «ه»,
    # «ه.», «٠» or «•» depending on the page.
    line = re.sub(r"^\s*(?:ه\.?|٠|•|·)\s+", "- ", line)

    return re.sub(r"[ \t]+", " ", line).strip()


def is_noise_line(line):
    """
    True for OCR debris: page furniture and header fragments that come
    through as disconnected letter clusters.

        «ا ال ال ال م»   «دن حص كن»   «60»

    Detected structurally, not by matching known junk: a line of several
    tokens none of which is longer than three characters is not Arabic
    prose. Real short phrases survive because one word is always longer —
    «لا يوجد» keeps «يوجد».

    This DELETES text, so it is deliberately narrow. Anything ambiguous is
    kept; shipping a stray fragment is better than deleting a real fee.
    """
    if re.fullmatch(r"[\d\s.،]+", line):        # a bare page number
        return True
    tokens = line.split()
    if len(tokens) < 2:
        return False
    return all(len(t.strip(".،:-")) <= 3 for t in tokens)


def clean_block(lines):
    cleaned = [clean_line(line) for line in lines]
    return "\n".join(line for line in cleaned
                     if line and not is_noise_line(line)).strip()


def is_empty_field(text):
    if not text:
        return True
    stripped = re.sub(r"[\s.،:\-]", "", text)
    if not stripped:
        return True
    return any(marker in text for marker in EMPTY_MARKERS) and len(text) < 80


# --- Parsing --------------------------------------------------------------

def label_of(line):
    """
    Return (section, inline_value) for a field label, or None.

    Three shapes occur, all in the same document:

        الرسوم:                          label alone, value on later lines
        :الرسوم                          same, colon flipped by RTL OCR
        الرسوم: تختلف حسب نوع التصريح     label AND value on one line

    The third was being missed, which did real damage: the fee line stayed
    part of whatever section preceded it, so «الرسوم» came out empty and the
    fee was filed under «الشروط والمتطلبات».
    """
    s = clean_line(line)
    if s.startswith(":"):
        return (FIELDS.get(s[1:].strip()), "") if FIELDS.get(s[1:].strip()) else None

    head, sep, tail = s.partition(":")
    if not sep:
        return None
    section = FIELDS.get(head.strip())
    return (section, tail.strip()) if section else None


def parse(path):
    """
    Split one .ocr.txt into service records.

    Returns (services, unparsed) where `unparsed` lists lines that look like
    a service heading but produced no record — the audit that proves nothing
    was dropped between finding a heading and emitting it.
    """
    lines = path.read_text(encoding="utf-8").split("\n")

    marks, page_of = [], {}
    page = 1
    for i, line in enumerate(lines):
        page_marker = PAGE.search(line)
        if page_marker:
            page = int(page_marker.group(1))
        page_of[i] = page

        auth = AUTH_LABELLED.search(line) or AUTH_NUMBERED.match(line)
        if auth:
            marks.append((i, "auth", auth.group(1).strip()))
            continue
        service = SERVICE.search(line)
        if service:
            marks.append((i, "svc", service.group(2).strip(), service.group(1)))

    parsed_lines = {m[0] for m in marks if m[1] == "svc"}
    unparsed = [line.strip() for i, line in enumerate(lines)
                if SERVICE_LOOSE.search(line) and i not in parsed_lines]

    services, authority = [], None
    for idx, mark in enumerate(marks):
        if mark[1] == "auth":
            authority = mark[2]
            continue

        line_no, _, name, number = mark
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)

        fields, current = {}, None
        for i in range(line_no + 1, end):
            if PAGE.search(lines[i]):
                continue
            labelled = label_of(lines[i])
            if labelled:
                current, inline = labelled
                fields.setdefault(current, [])
                if inline:
                    fields[current].append(inline)
            elif current and lines[i].strip():
                fields[current].append(lines[i])

        services.append({
            "authority_raw": authority,
            "name": clean_line(name),
            "number": number,
            "page": page_of.get(line_no, 1),
            "sections": {k: clean_block(v) for k, v in fields.items()},
            "source_file": path.name,
        })
    return services, unparsed


# --- Gate -----------------------------------------------------------------

GENERATED_ID = re.compile(r"^[a-z]+-\d{2}(?:-\d+)?$")


def already_curated(out_dir):
    """
    Normalised names of services a HUMAN already wrote up.

    The first pass imported 10 services that were already in the corpus by
    hand — «طلب تخصيص قطعة أرض لبناء مسجد» existed as both
    mara-mosque-land-allocation and mara-01. Two copies of one service is
    not redundancy, it is damage: retrieval spends its window twice on the
    same text, and the answer cites one service twice as though two sources
    agreed.

    The hand-written copy wins. It has a real source_url, and someone read
    it. Files whose id looks generated are skipped so re-running this
    importer stays idempotent rather than treating its own output as
    curated.
    """
    curated = {}
    for path in Path(out_dir).glob("*.md"):
        if GENERATED_ID.match(path.stem):
            continue
        head = path.read_text(encoding="utf-8")[:800]
        name = re.search(r"^service:\s*(.+)$", head, re.M)
        if name:
            curated[normalise_name(name.group(1).strip().strip('"'))] = path.stem
    return curated


def assess(service, curated=None):
    """Return (ok, reason). A service is imported only if it is actionable."""
    if not service["authority_raw"]:
        return False, "no authority heading precedes this service"

    existing = (curated or {}).get(normalise_name(service["name"]))
    if existing:
        return False, f"already curated by hand as {existing}"

    # Only rejects headings that do not READ like an authority — an unknown
    # but well-formed ministry is imported and auto-registered instead.
    if match_authority(service["authority_raw"]) is None:
        return False, (f"not a recognisable authority heading: "
                       f"{service['authority_raw']!r}")

    for term in EXCLUDE:
        if term in service["name"]:
            return False, f"not a citizen transaction (matched {term!r})"

    substantive = [k for k, v in service["sections"].items()
                   if k in SECTION_ORDER and not is_empty_field(v)]
    if len(substantive) < MIN_FIELDS:
        return False, (f"only {len(substantive)} substantive field(s), "
                       f"need {MIN_FIELDS}")
    return True, f"{len(substantive)} fields"


# --- Emission -------------------------------------------------------------

def render(service, service_id, doc_title):
    auth_id, name_ar, name_en = match_authority(service["authority_raw"])

    # The only citation these documents can support. Specific enough to
    # check by hand: document, authority, service number, page.
    source_ref = (f"{doc_title} — {name_ar}، الخدمة رقم ({service['number']})"
                  f"، ص {service['page']}")

    front = {
        "authority": name_ar,
        "authority_en": name_en,
        "service": service["name"],
        "service_id": service_id,
        "source_ref": source_ref,
        "source_title": doc_title,
        "retrieved": date.today().isoformat(),
        "verified_by": "PENDING",
    }
    lines = ["---"]
    lines += [f'{k}: "{v}"' if any(c in str(v) for c in ':«»—') else f"{k}: {v}"
              for k, v in front.items()]
    lines += ["---", "", f"# {service['name']}", ""]

    for section in SECTION_ORDER:
        body = service["sections"].get(section)
        if body and not is_empty_field(body):
            lines += [f"## {section}", "", body, ""]
    return "\n".join(lines).rstrip() + "\n"


def update_registry(accepted, path=REGISTRY):
    """
    Declare what was just written, so it can actually be reached.

    Delegates to chatbot.rag.registry_sync, which reads the corpus
    DIRECTORY rather than this script's accepted-list. That indirection is
    deliberate. This function used to re-derive ids from the OCR itself,
    and the two copies drifted within the week: the importer moved to
    document-based numbering and this did not, so the registry named
    service_ids that no longer existed and every one of those services
    refused while sitting fully indexed.

    Reading the written files means there is one source of truth, and a
    hand-written markdown file dropped into the corpus registers on exactly
    the same path.
    """
    from chatbot.rag.registry_sync import sync
    return sync(OUT_DIR, path, write=True)


def main():
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Build corpus .md files from OCR text, with no LLM.")
    parser.add_argument("--write", action="store_true",
                        help="actually write files (default is a dry run)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing files in data/source_docs/")
    parser.add_argument("--ocr-dir", type=Path, default=OCR_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--doc-title", default="دليل الخدمات الحكومية",
                        help="cited as the source document")
    args = parser.parse_args()

    ocr_files = sorted(args.ocr_dir.glob("*.ocr.txt"))
    if not ocr_files:
        print(f"No .ocr.txt files in {args.ocr_dir}")
        print("Run scripts/ocr_inbox.py first.")
        return

    all_services, all_unparsed = [], []
    for path in ocr_files:
        services, unparsed = parse(path)
        all_unparsed.extend((path.name, line) for line in unparsed)
        all_services.extend(services)
        print(f"{path.name}: {len(services)} services parsed"
              + (f", {len(unparsed)} UNPARSED" if unparsed else ""))

    # Reconciliation against the LOOSE pattern, not against ourselves.
    #
    # An import that drops a service leaves it looking perfectly fine while
    # being unanswerable forever — the same silent-refusal shape as the
    # service_id mismatch that cost this project a debugging session. Found
    # one real case this way: OCR ate the closing paren of «الخدمة رقم (9:».
    if all_unparsed:
        print(f"\nABORT: {len(all_unparsed)} line(s) look like a service "
              f"heading but produced no record:\n")
        for filename, line in all_unparsed:
            print(f"  {filename}: {line[:90]}")
        print("\nFix the SERVICE pattern before importing — a dropped service "
              "is indexed nowhere and refuses every question.")
        sys.exit(1)

    curated = already_curated(args.out_dir)
    if curated:
        print(f"\n{len(curated)} hand-curated services already in the corpus; "
              f"any OCR duplicate of one will be skipped.")

    accepted, rejected, taken = [], [], set()
    for service in all_services:
        ok, reason = assess(service, curated)
        if not ok:
            rejected.append((service, reason))
            continue

        # Numbered from the DOCUMENT, not from acceptance order.
        #
        # Sequential numbering looked simpler until the duplicate check
        # landed: rejecting 10 services renumbered every id after them, so
        # the registry pointed at service_ids that no longer existed and
        # every one of those services would have refused. Anchoring the id
        # to «الخدمة رقم (N)» makes it stable under any change to the gate,
        # and traceable back to the page it came from.
        auth_id = match_authority(service["authority_raw"])[0]
        service_id = f"{auth_id}-{int(service['number']):02d}"
        suffix = 1
        while service_id in taken:            # the OCR does repeat numbers
            suffix += 1
            service_id = f"{auth_id}-{int(service['number']):02d}-{suffix}"
        taken.add(service_id)
        accepted.append((service, service_id))

    print(f"\n{'=' * 66}")
    print(f"parsed  : {len(all_services)}")
    print(f"accepted: {len(accepted)}")
    print(f"rejected: {len(rejected)}")

    by_authority = Counter(sid.rsplit("-", 1)[0] for _, sid in accepted)
    print("\nservices per authority:")
    for auth_id, count in sorted(by_authority.items(), key=lambda x: -x[1]):
        print(f"  {auth_id:<8} {count}")

    reasons = Counter(reason.split("(")[0].split(":")[0].strip()
                      for _, reason in rejected)
    print("\nrejection reasons:")
    for reason, count in reasons.most_common():
        print(f"  {count:>3}  {reason}")

    # Registry reconciliation — a covered authority is a precondition, not a
    # nicety: L1 refuses an uncovered authority BEFORE retrieval runs, so an
    # imported service under one is indexed and permanently unreachable.
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    covered = {a["id"] for a in registry["covered"]}
    needed = set(by_authority) - covered
    if needed:
        print(f"\n!! {len(needed)} authorities are NOT in the registry's covered "
              f"list: {', '.join(sorted(needed))}")
        print("   Their services would be refused before retrieval runs.")
        print("   Add them to data/registry/authorities.json.")

    if not args.write:
        print(f"\nDry run. Nothing written. Re-run with --write.")
        return

    args.out_dir.mkdir(parents=True, exist_ok=True)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    written, skipped = 0, 0
    for service, service_id in accepted:
        target = args.out_dir / f"{service_id}.md"
        content = render(service, service_id, args.doc_title)
        if target.exists() and not args.force:
            (REVIEW_DIR / f"{service_id}.ocr.md").write_text(content, encoding="utf-8")
            skipped += 1
            continue
        target.write_text(content, encoding="utf-8")
        written += 1

    for service, reason in rejected:
        name = re.sub(r"[^\w؀-ۿ]+", "-", service["name"])[:60]
        (REVIEW_DIR / f"REJECTED-{name}.md").write_text(
            f"# {service['name']}\n\nrejected: {reason}\n"
            f"authority: {service['authority_raw']}\n"
            f"source: {service['source_file']}, page {service['page']}\n\n"
            + "\n".join(f"## {k}\n\n{v}\n" for k, v in service["sections"].items()),
            encoding="utf-8")

    print(f"\nwritten : {written} -> {args.out_dir}")
    print(f"skipped : {skipped} (existed; copies in {REVIEW_DIR})")
    print(f"rejected: {len(rejected)} written to {REVIEW_DIR} for review")

    from chatbot.rag.registry_sync import format_report

    summary = update_registry(accepted)
    print()
    print(format_report(summary, wrote=True))

    print("\nNext: python scripts/build_index.py")


if __name__ == "__main__":
    main()
