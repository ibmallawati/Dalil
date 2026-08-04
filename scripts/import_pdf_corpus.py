"""
import_pdf_corpus.py — build corpus files from the teammate-authored
ministry PDF (الوزارات.pdf).

    python scripts/import_pdf_corpus.py --dry-run     # inventory + filter only
    python scripts/import_pdf_corpus.py               # build the .md files

STANDALONE. Same validator as the web scraper, so PDF-sourced content is
held to exactly the same standard as scraped content.

--- Why this reads OCR and pypdf together -------------------------------

pypdf's Arabic from this PDF is CORRUPTED: 4,663 fathatan against 3,721
spaces, because U+064B is standing in for the space character. D3 strips
tashkeel, so that text entering the corpus would fuse every word and break
retrieval with no visible error.

Tesseract's Arabic is clean. But Tesseract was run with lang="ara" and
therefore dropped the Latin — including every URL.

So: Arabic from OCR, URLs from pypdf, aligned by page number. Neither
source alone is usable.

--- The usefulness filter -----------------------------------------------

The PDF documents 44 "services". Many are not citizen transactions —
contact forms, a staff social club, a live broadcast page, internal
treasury operations. Indexing those is not merely wasteful, it is harmful:

  1. they still get embedded, and compete in similarity search on
     authority-name overlap alone;
  2. once matched, whole-service loading feeds D5 six empty sections and
     the card renders six «غير محدد في المصدر» rows — which a citizen
     reads as "this service is free and needs no documents", an answer
     rather than an absence;
  3. they can SHADOW a real service: a vague question about the Ministry
     of Defence that lands on «التواصل مع وزارة الدفاع» returns a contact
     form instead of a procedure;
  4. they inflate the service count quoted to a panel while adding nothing
     answerable.

So a service is imported only when it is ACTIONABLE — it must have a
citable URL, at least three populated fields, and not be on the excluded
list below.
"""

import argparse
import io
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pypdf import PdfReader

from chatbot.config import DATA_DIR
from chatbot.llm import chat, usage_of
from chatbot.logging_config import configure_logging
from chatbot.scraper.validator import summarise, validate

PDF = Path("الوزارات.pdf")
OCR = DATA_DIR / "pdf_ocr" / "الوزارات.ocr.txt"
OUT_DIR = DATA_DIR / "source_docs"
REVIEW_DIR = DATA_DIR / "needs_review"
MODEL = "gemini-3.1-flash-lite"

MIN_SCORE = 3

# Not citizen transactions. Excluded by name regardless of how many fields
# they happen to fill.
EXCLUDE = [
    "تواصل", "نادي الشفق", "البث المباشر", "تحميل الإصدارات",
    "الشكاوى والبلاغات", "البيانات المفتوحة",
    "منصة حزم", "إنجاز", "نظام خدمات المستفيدين",
    # Internal treasury / government-to-government, not public-facing.
    "أوامر الشراء", "الاحتياجات المالية للشركات الحكومية", "إصدار الضمانات",
    "القروض التنموية", "شهادات ضريبية لشركات الامتياز",
    "الإيراد والصرف المالي",
]

EMPTY_MARKERS = ["لا توجد", "لا يوجد", "غير محددة", "غير محدد", "لم يتم",
                 "لا تتوفر", "غير منشور", "غير معلن"]

LABELS = ("الفئة المستفيدة|خطوات الحصول على الخدمة|المستندات المطلوبة|"
          "الشروط والمتطلبات|الرسوم|مدة الإنجاز|مدة إنجاز الخدمة|"
          "رابط الخدمة|وصف الخدمة")
# OCR of RTL text puts the colon BEFORE the label, on its own line.
LABEL_RE = re.compile(rf"^\s*[:：]\s*({LABELS})\s*$", re.M)
HEAD_RE = re.compile(r"(?:الخدمة/القسم|الخدمة|القسم)\s*رقم\s*\(?\s*\d+\s*\)?\s*[:：]\s*([^\n]{3,70})")
AUTHORITY_RE = re.compile(r"اسم الجهة\s*[:：]\s*(وزارة[^\n\-‏]{3,60})")

PROMPT = """أنت أداة إعادة هيكلة نصوص. النص المرفق مستخرج بتقنية OCR من دليل
خدمات حكومي عماني، وقد تكون علامات الترقيم في غير مواضعها.

أعد كائن JSON واحداً فقط، دون أي نص آخر ودون علامات ```.

الحقول:
- service: اسم الخدمة بالعربية
- service_id: معرّف بالأحرف اللاتينية الصغيرة والشرطات فقط
- sections: مصفوفة من {"heading": "...", "body": "..."}

العناوين المسموح بها فقط، بهذا الترتيب:
الفئة المستهدفة، المستندات المطلوبة، الخطوات، الرسوم، المدة المتوقعة،
أين تتم المعاملة، ملاحظات

قواعد صارمة:
1. استخدم النص المرفق فقط. لا تضف أي معلومة من معرفتك العامة إطلاقاً.
2. إذا لم يذكر النص معلومة قسمٍ ما، أو ذكر أنها «لا توجد» أو «غير محددة»،
   احذف ذلك القسم بالكامل. لا تكتب «غير محدد» ولا أي عبارة بديلة.
3. انقل الأرقام كما وردت حرفياً. لا تضف وحدة عملة لم ترد في النص.
4. صحّح مواضع الترقيم وترتيب أرقام القوائم فقط. لا تغيّر الكلمات.
5. لا تلخّص الخطوات أو الشروط. انقلها كاملة.
"""


def load_blocks():
    """[(authority, service_name, block_text, url)] for every service."""
    ocr = OCR.read_text(encoding="utf-8")
    parts = re.split(r"===== PAGE (\d+) =====", ocr)
    by_page = {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}

    flat, spans = "", []
    for pg in sorted(by_page):
        spans.append((len(flat), pg))
        flat += by_page[pg]

    def page_of(pos):
        page = 1
        for start, pg in spans:
            if start <= pos:
                page = pg
        return page

    reader = PdfReader(str(PDF))

    def urls_near(page):
        found = []
        for p in range(page, min(page + 2, len(reader.pages) + 1)):
            for u in re.findall(r"https?://[^\s\)\]]+",
                                reader.pages[p - 1].extract_text() or ""):
                path = re.sub(r"https?://[^/]+", "", u).strip("/")
                if len(path) > 2 and u not in found:
                    found.append(u)
        return found

    authorities = [(m.start(), m.group(1).strip()) for m in AUTHORITY_RE.finditer(flat)]

    def authority_of(pos):
        cur = None
        for start, name in authorities:
            if start <= pos:
                cur = name
        return cur

    heads = [(m.start(), m.group(1).strip()) for m in HEAD_RE.finditer(flat)]
    blocks = []
    for i, (pos, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(flat)
        found = urls_near(page_of(pos))
        blocks.append({
            "authority": authority_of(pos), "name": name,
            "text": flat[pos:end], "url": found[0] if found else None,
        })
    return blocks


def score(block):
    """How many of the five substantive fields carry real content."""
    marks = [(m.start(), m.end(), m.group(1)) for m in LABEL_RE.finditer(block["text"])]
    values = {}
    for i, (start, end, label) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(block["text"])
        values[label] = block["text"][end:nxt].strip()

    def populated(label):
        v = values.get(label, "")
        return bool(v) and len(v) > 12 and not any(e in v for e in EMPTY_MARKERS)

    return sum(populated(l) for l in (
        "المستندات المطلوبة", "خطوات الحصول على الخدمة", "الرسوم",
        "الشروط والمتطلبات", "الفئة المستفيدة"))


def excluded(name):
    return any(x in name for x in EXCLUDE)


def slug(text, fallback):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "")).strip("-").lower()
    return text or fallback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="show the filter decision without calling the model")
    parser.add_argument("--min-score", type=int, default=MIN_SCORE)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    configure_logging()
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    blocks = load_blocks()
    for b in blocks:
        b["score"] = score(b)
        if excluded(b["name"]):
            b["verdict"] = "EXCLUDED (not a citizen transaction)"
        elif not b["url"]:
            b["verdict"] = "EXCLUDED (no citable URL)"
        elif b["score"] < args.min_score:
            b["verdict"] = f"EXCLUDED (only {b['score']} populated fields)"
        else:
            b["verdict"] = "IMPORT"

    keep = [b for b in blocks if b["verdict"] == "IMPORT"]
    print(f"{'service':<46}{'auth':<26}{'sc':>3}  verdict")
    print("-" * 110)
    for b in sorted(blocks, key=lambda x: (x["verdict"] != "IMPORT", -x["score"])):
        print(f"{b['name'][:44]:<46}{(b['authority'] or '?')[:24]:<26}"
              f"{b['score']:>3}  {b['verdict']}")
    print(f"\n{len(blocks)} services found -> {len(keep)} to import")

    if args.dry_run:
        json.dump([{k: v for k, v in b.items() if k != "text"} for b in blocks],
                  open(REVIEW_DIR / "pdf1_filter.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"-> {REVIEW_DIR / 'pdf1_filter.json'}")
        return 0

    passed, failed = [], []
    for i, b in enumerate(keep, 1):
        print(f"\n[{i}/{len(keep)}] {b['name']}")
        try:
            completion = chat(
                [{"role": "system", "content": PROMPT},
                 {"role": "user", "content": f"الجهة: {b['authority']}\n\n{b['text'][:12000]}"}],
                model=MODEL, max_tokens=4000)
        except Exception as exc:
            print(f"    ERROR {type(exc).__name__}: {exc}")
            failed.append((b["name"], f"llm: {type(exc).__name__}"))
            continue

        usage = usage_of(completion)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "",
                      (completion.choices[0].message.content or "").strip(), flags=re.DOTALL)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(match.group(0)) if match else None
        if not data:
            print("    FAIL — unparseable model output")
            failed.append((b["name"], "unparseable"))
            continue

        service_id = slug(data.get("service_id"), slug(b["name"], f"service-{i}"))
        body = "\n\n".join(
            f"## {s.get('heading','').strip()}\n\n{s.get('body','').strip()}"
            for s in data.get("sections", []) if s.get("heading") and s.get("body"))

        findings = validate(body, b["text"], data.get("service", ""))
        if findings:
            (REVIEW_DIR / f"{service_id}.FAILED.txt").write_text(
                f"service : {b['name']}\nurl     : {b['url']}\n\n" + summarise(findings),
                encoding="utf-8")
            print(f"    FAIL — {len(findings)} finding(s) -> needs_review/")
            for f in findings[:3]:
                print(f"           {f}")
            failed.append((b["name"], f"{len(findings)} findings"))
            continue

        domain = urlparse(b["url"]).netloc
        front = "\n".join([
            "---",
            f"authority: {b['authority']}",
            f"authority_en: {data.get('authority_en','')}",
            f"service: {data.get('service', b['name'])}",
            f"service_id: {service_id}",
            f"source_url: {b['url']}",
            f'source_title: "{b["name"]}"',
            f"retrieved: {date.today().isoformat()}",
            "verified_by: PENDING",
            "extraction_verified: auto",
            f"source_domain: {domain}",
            "provenance: pdf-ocr",
            "---", "",
        ])
        target = OUT_DIR / f"{service_id}.md"
        if target.exists() and not args.force:
            (REVIEW_DIR / f"{service_id}.PDF.md").write_text(front + body + "\n", encoding="utf-8")
            print(f"    SKIP — exists; wrote needs_review/{service_id}.PDF.md")
            failed.append((b["name"], "target exists"))
            continue
        target.write_text(front + body + "\n", encoding="utf-8")
        print(f"    PASS -> {target.name}  ({len(data.get('sections',[]))} sections, "
              f"{usage.get('completion_tokens')} tok)")
        passed.append(service_id)

    print(f"\n{'='*70}\nPASSED {len(passed)}   FAILED {len(failed)}")
    for name, why in failed:
        print(f"  - {name}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
