"""
scrape_corpus.py — turn a list of URLs into corpus .md files.

    python scripts/scrape_corpus.py urls.txt
    python scripts/scrape_corpus.py urls.txt --model gemini-3.1-flash-lite
    python scripts/scrape_corpus.py urls.txt --force        # allow overwrite

STANDALONE. Nothing in the request path imports this. It writes files that
scripts/build_index.py later reads, and that is the only coupling.

--- The pipeline ---------------------------------------------------------

  robots.txt  ->  fetch  ->  save raw text  ->  LLM restructure
                               ^                      |
                               |                      v
                               +---- VALIDATOR <------+
                                          |
                        PASS -> data/source_docs/
                        FAIL -> data/needs_review/ + a .txt saying why

The raw text is written to disk BEFORE the model is called. That ordering
is deliberate: the raw file is the evidence the validator checks against,
and it must exist independently of whatever the model returns. If the
model fabricates a fee, the proof that it fabricated it is already saved.

--- Robots -----------------------------------------------------------------

robots.txt is checked before every fetch and a disallow is final. There is
no override flag, because the one site we know blocks bots
(business.gov.om) is a site we have decided not to circumvent. Adding a
switch would make that a decision someone could take by accident.

--- URL LIST FORMAT --------------------------------------------------------

One URL per line. Blank lines and '#' comments ignored.

A page can describe several services (the ROP civil-status page covers new
ID, ID renewal and resident card on one URL). Rather than have the model
decide how to split a page — a judgement it would make differently every
run — list the URL once per service and name the service after a pipe:

    https://www.rop.gov.om/arabic/e_id.aspx | إصدار البطاقة الشخصية لأول مرة
    https://www.rop.gov.om/arabic/e_id.aspx | تجديد البطاقة الشخصية

The hint tells the model which service on the page to extract, and becomes
part of the output slug so the two runs do not collide. Without a hint the
model takes the page's primary service.
"""

import argparse
import io
import json
import re
import sys
import time
import unicodedata
import urllib.robotparser
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from chatbot.config import DATA_DIR
from chatbot.llm import chat, usage_of
from chatbot.logging_config import configure_logging
from chatbot.scraper.validator import summarise, validate

# Identifies us honestly, and gives an operator somewhere to complain to.
# A scraper that hides what it is has already decided it is doing something
# it should not.
USER_AGENT = (
    "DaleelCorpusBot/1.0 (Omani government services assistant; "
    "academic project for the MTCIT 'Engineer it with AI' competition; "
    "+https://gov.om)"
)

# The file a human edits to say what to scrape. Not to be confused with
# robots.txt, which is published by each REMOTE site to say what a crawler
# is allowed to fetch — we only ever read that, never write it.
DEFAULT_URL_FILE = DATA_DIR / "scrape_urls.txt"

RAW_DIR = DATA_DIR / "scraped_raw"
OUT_DIR = DATA_DIR / "source_docs"
REVIEW_DIR = DATA_DIR / "needs_review"

# A fallback model by default. The demo model's daily quota is a scarce,
# per-day resource and a scraping run would eat it.
DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_DELAY = 3.0

STRIP_TAGS = ["script", "style", "nav", "header", "footer", "noscript", "svg", "form"]

PROMPT = """أنت أداة إعادة هيكلة نصوص. مهمتك تحويل نص صفحة خدمة حكومية عمانية
إلى أقسام منظمة.

أعد كائن JSON واحداً فقط، دون أي نص آخر ودون علامات ```.

الحقول:
- service: اسم الخدمة بالعربية كما ورد في النص
- authority: اسم الجهة بالعربية كما ورد في النص
- authority_en: اسم الجهة بالإنجليزية
- service_id: معرّف بالأحرف اللاتينية الصغيرة والشرطات فقط، مثل rop-passport-renewal
- source_title: عنوان الصفحة كما ورد
- sections: مصفوفة من {"heading": "...", "body": "..."}

العناوين المسموح بها في sections، بهذا الترتيب فقط:
الفئة المستهدفة، المستندات المطلوبة، الخطوات، الرسوم، المدة المتوقعة،
أين تتم المعاملة، ملاحظات

قواعد صارمة لا يجوز خرقها:
1. استخدم النص المرفق فقط. لا تضف أي معلومة من معرفتك العامة إطلاقاً.
2. إذا لم يذكر النص معلومة قسمٍ ما، احذف ذلك القسم بالكامل من المصفوفة.
   لا تكتب «غير محدد» ولا «لا يوجد» ولا أي عبارة بديلة.
3. انقل الأرقام كما وردت حرفياً. إذا ذُكر رقم دون وحدة عملة، اكتبه دون
   وحدة. لا تضف «ريال» أو أي وحدة لم ترد في النص.
4. إذا كان النص يذكر مبلغاً دون توضيح سببه، انقله كما هو دون تفسير.
5. إذا كانت الرسوم في جدول، أعد إنتاج الجدول بصيغة markdown داخل body.
6. لا تلخّص الشروط أو الخطوات. انقلها كاملة.
"""


def slugify(text, fallback="service"):
    """ASCII slug. service_id must equal the filename, so it must be ASCII."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or fallback


def read_url_list(path):
    """[(url, service_hint or None)] from the list file."""
    entries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            url, hint = line.split("|", 1)
            entries.append((url.strip(), hint.strip()))
        else:
            entries.append((line, None))
    return entries


_robots_cache = {}


def robots_allows(url):
    """(allowed, reason). A disallow is final — there is no override."""
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root not in _robots_cache:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{root}/robots.txt")
        try:
            parser.read()
        except Exception as exc:
            # Unreachable robots.txt is not permission. Treat it as a block
            # and say so, rather than assuming consent.
            _robots_cache[root] = ("unreadable", str(exc)[:120])
        else:
            _robots_cache[root] = ("ok", parser)
    state, payload = _robots_cache[root]
    if state == "unreadable":
        return False, f"robots.txt unreadable ({payload}) — treated as disallow"
    return (payload.can_fetch(USER_AGENT, url) or payload.can_fetch("*", url),
            "disallowed by robots.txt")


def fetch_text(url, timeout=30):
    """Fetch and reduce to visible text. Raises on HTTP error."""
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def structure(raw_text, url, hint, model):
    """One LLM call. Returns (parsed_dict, usage) or (None, usage)."""
    ask = f"رابط الصفحة: {url}\n"
    if hint:
        ask += (f"\nاستخرج هذه الخدمة تحديداً من الصفحة: «{hint}»\n"
                f"تجاهل الخدمات الأخرى المذكورة في نفس الصفحة.\n")
    ask += f"\nنص الصفحة:\n{raw_text[:24000]}"

    completion = chat(
        [{"role": "system", "content": PROMPT}, {"role": "user", "content": ask}],
        model=model, max_tokens=4000,
    )
    usage = usage_of(completion)
    text = (completion.choices[0].message.content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None, usage
    try:
        return json.loads(match.group(0)), usage
    except json.JSONDecodeError:
        return None, usage


def build_markdown(data, url, service_id):
    """Assemble the .md. Front-matter is machine-built, never model-supplied."""
    domain = urlparse(url).netloc
    body = "\n\n".join(
        f"## {s.get('heading','').strip()}\n\n{s.get('body','').strip()}"
        for s in data.get("sections", [])
        if s.get("heading") and s.get("body")
    )
    front = [
        "---",
        f"authority: {data.get('authority','')}",
        f"authority_en: {data.get('authority_en','')}",
        f"service: {data.get('service','')}",
        f"service_id: {service_id}",
        f"source_url: {url}",
        f'source_title: "{str(data.get("source_title","")).replace(chr(34), "")}"',
        f"retrieved: {date.today().isoformat()}",
        # Human verification is a quality upgrade, not a gate — the
        # validator is the gate.
        "verified_by: PENDING",
        # Set by the validator, never by the model.
        "extraction_verified: auto",
        f"source_domain: {domain}",
        "---",
        "",
    ]
    return "\n".join(front) + body + "\n", body


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # Defaults to the checked-in list so the common case is a bare
    # `python scripts/scrape_corpus.py` with no argument to remember.
    parser.add_argument("url_file", nargs="?", default=str(DEFAULT_URL_FILE),
                        help=f"URL list, one per line (default: {DEFAULT_URL_FILE.name})")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"structuring model (default {DEFAULT_MODEL}). "
                             f"Do NOT use the demo model.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"seconds between requests (default {DEFAULT_DELAY})")
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing files in data/source_docs/")
    args = parser.parse_args()

    configure_logging()
    for d in (RAW_DIR, OUT_DIR, REVIEW_DIR):
        d.mkdir(parents=True, exist_ok=True)

    entries = read_url_list(args.url_file)
    print(f"urls    : {len(entries)}")
    print(f"model   : {args.model}")
    print(f"agent   : {USER_AGENT[:60]}...")
    print("=" * 76)

    passed, failed, blocked, errored = [], [], [], []
    registry_suggestions = []
    total_tokens = 0

    for index, (url, hint) in enumerate(entries, 1):
        label = f"{url}" + (f"  |  {hint}" if hint else "")
        print(f"\n[{index}/{len(entries)}] {label}")

        allowed, reason = robots_allows(url)
        if not allowed:
            print(f"    BLOCKED — {reason}")
            blocked.append((label, reason))
            continue

        try:
            raw = fetch_text(url)
        except Exception as exc:
            print(f"    ERROR — fetch failed: {type(exc).__name__}: {exc}")
            errored.append((label, f"fetch: {type(exc).__name__}"))
            continue

        base = slugify(Path(urlparse(url).path).stem or urlparse(url).netloc)
        if hint:
            base = f"{base}--{slugify(hint)[:40]}"

        # Written BEFORE the model sees anything. This file is the evidence.
        raw_path = RAW_DIR / f"{base}.txt"
        raw_path.write_text(raw, encoding="utf-8")
        print(f"    raw    : {len(raw)} chars -> {raw_path.name}")

        try:
            data, usage = structure(raw, url, hint, args.model)
        except Exception as exc:
            print(f"    ERROR — model call failed: {type(exc).__name__}: {exc}")
            errored.append((label, f"llm: {type(exc).__name__}"))
            continue

        total_tokens += usage.get("completion_tokens", 0) + usage.get("prompt_tokens", 0)
        print(f"    tokens : prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')} "
              f"served_by={usage.get('served_by')}")

        if not data:
            print("    FAIL   — model output was not parseable JSON")
            (REVIEW_DIR / f"{base}.FAILED.txt").write_text(
                "The structuring model did not return parseable JSON.\n"
                f"url: {url}\nraw: {raw_path}\n", encoding="utf-8")
            failed.append((label, "unparseable model output"))
            continue

        service_id = slugify(data.get("service_id"), fallback=base)
        markdown, body = build_markdown(data, url, service_id)

        findings = validate(body, raw, data.get("service", ""))

        if findings:
            note = REVIEW_DIR / f"{service_id}.FAILED.txt"
            (REVIEW_DIR / f"{service_id}.md").write_text(markdown, encoding="utf-8")
            note.write_text(
                f"url : {url}\nraw : {raw_path}\nfile: {service_id}.md\n\n"
                + summarise(findings), encoding="utf-8")
            print(f"    FAIL   — {len(findings)} finding(s) -> "
                  f"needs_review/{service_id}.md")
            for f in findings[:4]:
                print(f"             {f}")
            if len(findings) > 4:
                print(f"             ... and {len(findings) - 4} more")
            failed.append((label, f"{len(findings)} validation finding(s)"))
            continue

        target = OUT_DIR / f"{service_id}.md"
        if target.exists() and not args.force:
            # Refusing by default protects hand-written files, which are
            # the higher-quality artifact.
            alt = REVIEW_DIR / f"{service_id}.SCRAPED.md"
            alt.write_text(markdown, encoding="utf-8")
            print(f"    SKIP   — {target.name} exists. Wrote {alt.name} instead "
                  f"(use --force to overwrite)")
            failed.append((label, "target exists, not overwritten"))
            continue

        target.write_text(markdown, encoding="utf-8")
        print(f"    PASS   -> source_docs/{target.name} "
              f"({len(data.get('sections', []))} sections)")
        passed.append((label, service_id))
        registry_suggestions.append({
            "service_id": service_id,
            "name_ar": data.get("service", ""),
            "authority_ar": data.get("authority", ""),
            "authority_en": data.get("authority_en", ""),
            "source_url": url,
            "variants": ["REVIEW: add the words a citizen would actually type"],
        })

        if index < len(entries):
            time.sleep(args.delay)

    # Suggestions go to a separate file for review. Auto-merging into the
    # registry would let a scraped file silently change routing.
    if registry_suggestions:
        path = REVIEW_DIR / "registry_suggestions.json"
        path.write_text(json.dumps(registry_suggestions, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"\nregistry suggestions -> {path}  (NOT merged; review and paste)")

    print("\n" + "=" * 76)
    print(f"PASSED  {len(passed)}   FAILED  {len(failed)}   "
          f"BLOCKED {len(blocked)}   ERRORED {len(errored)}")
    print(f"tokens  {total_tokens}")
    for title, rows in (("BLOCKED", blocked), ("ERRORED", errored), ("FAILED", failed)):
        if rows:
            print(f"\n{title}:")
            for label, reason in rows:
                print(f"  - {label}\n      {reason}")
    return 0 if not errored else 1


if __name__ == "__main__":
    sys.exit(main())
