# CORPUS_SPEC — عقد تسليم محتوى دليل

**Delivery contract for the Daleel corpus.** Read this before writing any service file.

This is the format `scripts/build_index.py` validates. A file that does not follow it
**fails the build** with a named error — it will not be silently skipped, and it will
not half-work.

---

## 1. The rules, in short

| Rule | Why |
|---|---|
| One file per service | One file = one `service_id` = one thing a citizen wants to do |
| Every file opens with YAML front-matter | `source_url`, which Daleel cites to the citizen, lives there |
| Split the body on `##` headers | One `##` section = one retrievable chunk |
| Arabic only in the body | Daleel v1 answers in Arabic; English belongs in `authority_en` |
| Never invent a fact | Every number, fee and step must exist on the official page |
| Never paraphrase a fee or a duration | Copy them exactly as published |
| Keep each `##` section under ~350 words | Longer sections get **silently truncated** by the embedding model |

> **The single most important rule:** if the official page does not say it, it does not
> go in the file. An answer Daleel cannot ground is worse than an answer it refuses to
> give — a wrong government procedure costs a citizen a wasted trip.

---

## 2. File naming

```
data/source_docs/<authority-slug>-<service-slug>.md
```

Examples:

```
data/source_docs/rop-passport-renewal.md
data/source_docs/rop-id-renewal.md
data/source_docs/rop-vehicle-registration-renewal.md
data/source_docs/rop-driving-licence.md
data/source_docs/mociip-commercial-registration.md
data/source_docs/mociip-licence-renewal.md
```

ASCII filenames only. The Arabic names live inside, in the front-matter.

---

## 3. Front-matter — all eight fields are required

The file must **start** with `---`. Only blank lines, a BOM, or an HTML comment
(`<!-- ... -->`) may precede it — those are harmless editor artefacts and are tolerated.

Anything else above the `---` is an **error**, including a heading. That case means the
author misunderstood the format, and the fields they believe they set would be silently
ignored.

```yaml
---
authority: شرطة عمان السلطانية
authority_en: Royal Oman Police
service: تجديد جواز السفر
service_id: rop-passport-renewal
source_url: https://www.rop.gov.om/...
source_title: تجديد جواز السفر العماني
retrieved: 2026-07-31
verified_by: اسم الشخص
---
```

| Field | Meaning | Notes |
|---|---|---|
| `authority` | Arabic name of the authority | Must match a covered authority in `data/registry/authorities.json` |
| `authority_en` | English name | Used in logs and when explaining coverage to the panel |
| `service` | Arabic name of the service | As the citizen would recognise it |
| `service_id` | stable slug | **Must equal the filename without `.md`.** The build fails otherwise |
| `source_url` | **full** URL of the official page | **Daleel cites this to the citizen.** Must start with `http` |
| `source_title` | title of the official page | As published |
| `retrieved` | ISO date `YYYY-MM-DD` | The day you copied the content |
| `verified_by` | who checked it | A real name. `PENDING` builds, but logs a warning |

### `service_id` is a join key, not a label

`service_id` is what connects this file to `data/registry/authorities.json`. The router
resolves a `service_id` from the registry, then asks the corpus for chunks carrying that
exact string.

One character of drift breaks it silently. On the first real corpus file, `service_id`
was written `rop_passport_renewal` while the registry said `rop-passport-renewal`. The
build passed, the file indexed cleanly, and the only symptom was that the service
refused every question — with the correct file sitting right there.

The build now rejects a mismatch, and prints a registry reconciliation showing which
declared services have content and which do not.

**Two services may share one `source_url`** — new issuance and renewal both live on the
same official ROP page. That is fine: they are separate files with separate
`service_id`s, and each cites the same URL.

**`source_url` is not decoration.** D5 renders it as the answer's citation, which is how
Daleel demonstrates it is not hallucinating. A file with a homepage URL instead of the
actual service page produces an answer that cites the wrong thing.

---

## 4. Body structure — one `##` per topic

Use these headings, in this order. They map directly onto the fields of the structured
answer card, so **using different headings degrades the output.** Omit a section only
when the official page genuinely has nothing for it.

```markdown
# تجديد جواز السفر

## الفئة المستهدفة
من يحق له استخدام هذه الخدمة.

## المستندات المطلوبة
- المستند الأول
- المستند الثاني

## الخطوات
1. الخطوة الأولى
2. الخطوة الثانية

## الرسوم
المبلغ بالريال العماني، كما هو منشور بالضبط.

## المدة المتوقعة
كم تستغرق المعاملة.

## أين تتم المعاملة
إلكترونيًا / في المركز / كلاهما — مع اسم الجهة أو التطبيق.

## ملاحظات
استثناءات أو حالات خاصة. اختياري.
```

### Why the headings matter

Each `##` section becomes **one chunk**, embedded on its own. A citizen asking
«كم رسوم تجديد الجواز؟» must hit the `## الرسوم` section of the right service.

The section is embedded with its document context automatically prepended —
`الجهة | الخدمة | ## القسم` — so a bare «الرسوم» heading is still retrievable. You do
not need to repeat the service name inside every section.

---

## 5. Distinct services, deliberate ambiguity

Two services that read alike are indistinguishable to retrieval. Write each one so its
sections say clearly what it *is* and what it is *not*.

**But do not eliminate ambiguity that real citizens actually have.** Commercial
registration and licence renewal are genuinely confused by real users, and that
collision is a designed demo case: it is what triggers Daleel's clarifying question

> «هل تقصد تسجيل سجل تجاري جديد، أم تجديد رخصة قائمة؟»

So write both files distinctly — and keep the overlap. The right handling of an
ambiguous question is to ask, not to guess.

### You must flag confusable pairs as you write

**This is a delivery requirement, not a suggestion.** While writing a service file, note
every *other* service a citizen could plausibly confuse it with, and report the pair.

Known candidates, almost certainly not the full list:

| | |
|---|---|
| تجديد جواز السفر | استخراج بدل فاقد/تالف للجواز |
| إصدار البطاقة الشخصية لأول مرة | تجديد البطاقة الشخصية |
| تجديد ملكية المركبة | نقل ملكية المركبة |
| تسجيل سجل تجاري جديد | تجديد الرخصة التجارية |

Each flagged pair becomes an entry in `data/registry/authorities.json` under
`ambiguous_phrases`, which is what makes Daleel **ask** instead of silently answering
about the wrong one:

> «هل تقصد تجديد جواز سفرك، أم استخراج بدل فاقد؟»

**Why you and not the code:** the system has two ways to detect ambiguity. It can be
*declared* in the registry — exact, explainable, fires identically every time — or
*inferred* at query time from two services scoring within a hair of each other, which is
a statistical guess that only fires sometimes.

Declared always beats inferred. But nothing can declare a confusion nobody wrote down,
and you are the only person who will see these pairs, because you are the one reading
both official pages side by side. A pair you notice and mention costs one line of JSON.
A pair nobody notices becomes a citizen confidently sent to the wrong procedure.

Send the pair, both `service_id`s, and a one-line Arabic question that would separate
them.

---

## 6. Before you commit a file

```bash
python scripts/build_index.py
```

The build will:

- **fail** with a named error if front-matter is missing, malformed, or `source_url`
  is not a URL
- **warn** if `verified_by` is still `PENDING`
- **warn** if a `##` section is long enough to be truncated by the embedding model
- print a per-service chunk count so you can see your file was picked up

A build that fails is telling you a file is wrong. Do not work around it.

---

## 7. Worked example

A complete, correctly-formatted file lives at:

```
docs/corpus-example/rop-passport-renewal.md
```

It sits **outside** `data/source_docs/` on purpose, so it is never indexed.

> **Its content is illustrative structure, not verified fact.** Copy the *shape*, then
> replace every value with what the official ROP page actually says. Do not copy its
> numbers.

---

## 8. Fixtures are not corpus

`data/fixtures/` holds invented stand-in content used to develop retrieval before the
real corpus existed. Those files carry `fixture: true` and the build **refuses** to
write them into the real index.

Never put a fixture in `data/source_docs/`, and never quote a number measured against
fixtures — accuracy figures reported to judges must come from the real corpus.
