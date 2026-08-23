# PDF intake — verbatim chunking

**Status:** BUILT as `scripts/import_ocr_corpus.py`, 2026-08-17.
`scripts/import_pdf_corpus.py` (the LLM-restructuring importer) is deleted.
**Date:** 2026-08-16, outcome appended 2026-08-17

## Outcome

All four §6 decisions were taken as recommended, and the importer ran.

| | Before | After |
|---|---:|---:|
| Corpus files | 27 | **94** |
| Indexed chunks | — | **430** |
| Registry authorities | 10 | **18** |
| Registry services | 26 | **93** |

98 services parsed, **73 accepted**, 25 rejected to `data/needs_review/`
(17 not citizen transactions, 8 too thin). Six ministries were promoted out
of `not_covered`; four were added. Verified end-to-end: «كيف أسجل طالب جديد
في المدارس الحكومية؟» — a Ministry of Education question that was refused
outright before — now answers, citing
«دليل الخدمات الحكومية — وزارة التربية والتعليم، الخدمة رقم (1)، ص 1».

Three things the build changed from what is specced below:

- **The reconciliation check had to audit against a LOOSER pattern**, not
  against itself. Comparing the parser to its own regex proves nothing: a
  heading the regex cannot see is invisible to both. Written that way, it
  immediately found a real case — OCR had eaten the closing paren of
  «الخدمة رقم (9:» and the service was being dropped silently.
- **Field labels occur with the value on the same line** («الرسوم: تختلف
  حسب نوع التصريح»), which §2 did not record. Handling it moved 6 services
  over the actionability bar, because fees were being filed under the
  preceding section and «الرسوم» came out empty.
- **Authority names are read from a canonical table**, so «سلطنة غُمان»
  never reaches front matter. Deriving them by unfolding the match key
  produced «وزاره العمل» — wrong, and displayed to citizens.

Still open: all 73 files are `verified_by: PENDING`, and the 25 rejects in
`data/needs_review/` have had no human pass.

Row 5 of the architecture proposal: replace `OCR → LLM restructure → validate → .md`
with `OCR → clean → chunk verbatim → index`. Rows 1–4 shipped; this one was
never agreed, and this document exists so it can be agreed or rejected on
evidence rather than on the one-line summary.

---

## 1. Why replace the current importer

**It rewrites content with an LLM.** During the real import the validator
rejected 5 of 15 services, every one the same failure — the model silently
repairing garbled OCR:

```
OCR source:  «.تختلة 1 نوع النشاط والتر خب»    ← garbled
model wrote: «تختلف حسب نوع النشاط والترخيص»    ← plausible invention
```

In one case the source's documents field was **empty** and the model wrote
one anyway. For government fees and required documents that is the failure
that sends a citizen to a counter without the right paper.

The validator caught these. But the validator only exists *because* of the
rewriting step. Remove the rewriting and most of the risk goes with it.

**It was also already non-runnable.** `import_pdf_corpus.py` read
`PDF = Path("الوزارات.pdf")`, and no PDF exists in this repo or its git
history — only the OCR output in `data/pdf_ocr/` survives. Whatever we
build must work from the `.ocr.txt` files, treating the PDF as optional.

---

## 2. What the OCR actually contains — measured

Both files parsed with a throwaway probe against
`data/pdf_ocr/الوزارات.ocr.txt` and `الوزارات 2.ocr.txt`.

| Measure | Value |
|---|---|
| Services detected | **97** |
| Services with ≥3 substantive fields | **74** |
| Distinct authorities | **17–18** |
| Services with a recoverable URL | **0** |
| Services under 2% non-Arabic characters | **93 / 97** |

Field-by-field yield across all 97:

| Field | Labelled | Has real content |
|---|---:|---:|
| `المستندات المطلوبة` | 95 | 74 |
| `خطوات الحصول على الخدمة` | 65 | 65 |
| `الفئة المستهدفة` | 56 | 54 |
| `الشروط والمتطلبات` | 54 | 45 |
| `مدة الإنجاز` | 46 | 36 |
| `الرسوم` | 43 | **16** |
| `وصف الخدمة` | 37 | 37 |
| `رابط الخدمة` | 22 | 22 (all garbage) |

**Read the fee row carefully.** Only 16 of 97 services carry an actual fee.
Fees are the most common demo question. This corpus is strong on steps and
documents, weak on money — worth knowing before it goes in front of judges.

### The structure is genuinely machine-detectable

- Services: `الخدمة رقم (N): <name>`, sometimes `الخدمة/القسم رقم (N):`.
  Numbering **restarts per authority**.
- Pages: `===== PAGE N =====`.
- Field labels are a closed set with known spelling variants.

### But it is dirty in specific, catalogued ways

1. **The colon precedes the label**, an RTL artifact: `:الرسوم` as often as
   `الرسوم:`. Both orientations occur in the same file.
2. **Two different authority heading conventions** — `اسم الجهة: وزارة …`
   *and* `5) وزارة الإسكان والتخطيط العمراني - سلطنة غمان`. A line-anchored
   regex on the first form silently drops the second. It dropped one
   authority and one service in the probe.
3. **RLM/LRM marks (`U+200F`, `U+200E`) mid-line**, including immediately
   before headings.
4. **Bullets OCR'd as the Arabic letter `ه`** — `ه السجل التجاري.`
5. **Sentence-final periods migrate to the start of the line** —
   `.أداء أدوارها الوطنية`
6. **List numbers migrate to the end** — `.الدخول إلى الموقع .1`
7. **`»` substituted for the Arabic comma `،`**
8. **Real OCR errors in words**, including `سلطنة غُمان` for `عُمان` — the
   name of the country, wrong, in 6 of 17 authority headings.
9. **URLs are destroyed beyond recovery**:
   `رابط الخدمة: مرمء. + مع دطاء<-ع1نا1117_50نا011172./ا013110/1.80ا. ممصرة//: دمخغخط`

---

## 3. Blockers that must be resolved first

These are not implementation details. Each one stops the pipeline dead.

### 3.1 The loader hard-requires a URL — and we have none

`markdown_corpus.REQUIRED_FIELDS` includes `source_url`, and
`validate_front_matter` rejects anything that is not `http(s)://`:

```python
url = str(meta["source_url"]).strip()
if not url.startswith(("http://", "https://")):
    raise CorpusError(...)
```

**Every imported file would fail the build.** Zero of 97 services have a
recoverable URL, and the source PDFs are gone, so the old
"Arabic from OCR, URLs from pypdf" trick cannot be repeated.

Row 6 of the proposal — *citation may be a URL **or** a text reference* —
was only half-implemented. `card.py` renders a textual `source_ref`
correctly and has tests for it, but **the corpus loader never emits
`source_ref`**, so that path is currently dead code for markdown chunks.

**Required change:** `source_url` becomes optional; a file must carry
`source_url` **or** `source_ref`, and at least one. This is the change that
unblocks everything else, and it is small.

Proposed citation string, built mechanically from what we do have:

```
source_ref: دليل الوزارات الحكومية — وزارة العمل، الخدمة رقم (4)، ص ٤٢
```

Page numbers are available: the parser tracks `===== PAGE N =====`.

### 3.2 Service IDs cannot be generated without an LLM

`service_id` must be a slug **equal to the filename stem** and is the join
key to the registry. Arabic service names cannot be mechanically turned
into meaningful English slugs without either an LLM or a transliteration
table — and using an LLM here reintroduces exactly what we removed.

**This matters far less than it looks, because of the Phase 2 L1 demotion.**
A service with no registry entry is still fully answerable: retrieval always
runs, and similarity finds it on its Arabic text. The registry entry only
buys the L1 hint.

**Recommendation:** mechanical `{authority_id}-{NN}` — `mol-04`,
`moe-01`. The slug is internal; the Arabic `service` field is what appears
in citations and in the answer. Hand-naming can be done later, service by
service, for the ones that turn out to matter.

### 3.3 Registry migration — this one is demo-visible

Mapping the 17 OCR authorities against `data/registry/authorities.json`:

| Registry status | Count | Authorities |
|---|---:|---|
| Already covered | 7 | الداخلية، المالية، التنمية الاجتماعية، التجارة والصناعة، الطاقة والمعادن، التراث والسياحة، الأوقاف |
| **`not_covered`** | 6 | التربية والتعليم، العمل، النقل والاتصالات، الثروة الزراعية، العدل، الخارجية |
| **Absent entirely** | 4 | الاقتصاد، الثقافة والرياضة والشباب، الإعلام، الدفاع |

The `not_covered` row is a **hard blocker, not a nicety**. L1 returns
`REFUSE_UNCOVERED` *before* retrieval runs:

```python
if match.verdict is Verdict.UNCOVERED_AUTHORITY:
    return RetrievalPlan(Strategy.REFUSE_UNCOVERED, match)
```

So an imported وزارة العمل service would sit fully indexed and be
**unreachable** — the exact silent-refusal failure mode that bit this
project once already with the `service_id` mismatch. Registry work on those
10 authorities — 6 promotions plus 4 new entries — is mandatory, and it
changes what Daleel refuses. That is a decision to make deliberately, not a
side effect of an import.

### 3.4 Duplicate authority across the two files

`وزارة الإعلام` appears in both PDFs with independent service numbering,
so `moi-01` style IDs collide. The importer must detect cross-file
collisions and fail rather than overwrite.

---

## 4. The design

```
data/pdf_ocr/*.ocr.txt
        │
        ├─ 1. parse    → authority / service / field / page   (regex, tolerant)
        │
        ├─ 2. clean    → mechanical, reversible, NO semantic edits
        │
        ├─ 3. gate     → drop non-actionable; report, never silently skip
        │
        └─ 4. emit     → data/source_docs/{id}.md  (front-matter + ## sections)
                              │
                              └─ scripts/build_index.py   (unchanged)
```

Nothing downstream changes. The output is ordinary corpus markdown, so the
existing loader, chunker, retrieval, L1 hint and citation rendering all
apply untouched.

### 4.1 What "verbatim" means precisely

**Permitted** — mechanical, deterministic, inspectable, no meaning changes:

- strip page markers, RLM/LRM, tatweel
- NFKC, which folds Arabic presentation forms (`ﻣﻼﺣﻈﺔ` → `ملاحظة`) back to
  normal letters. Verified against `chatbot.rag.arabic.normalize_arabic`.
  Needed because the *stored* text is what a citizen reads, and only the
  *embedding* text is normalised today
- normalise the label colon to one orientation
- `ه` at line-start → `-` list marker
- move a leading `.` to the end of its line; move a trailing `.N` to the front
- `»` → `،`
- collapse runs of whitespace

**Forbidden** — anything that requires understanding the text:

- fixing a misspelled word, **including `غُمان` → `عُمان`**
- completing a truncated sentence
- filling an empty field
- rephrasing, summarising, reordering
- adding a currency unit that is not present
- inferring a URL

Every rule above is a pure string transform with no model in the loop. That
is the whole point: **there is no rewriting step, so there is nothing to
validate.** The validator is retired, not replaced.

`غُمان` shipping uncorrected is a deliberate consequence. A rule that fixes
it is a rule that can fix other things, and "the model fixes obvious typos"
is precisely how `«تختلف حسب نوع النشاط والترخيص»` got invented.

### 4.2 The actionability gate

Carried over from the current importer, which got this right. A service is
imported only when it has **≥3 fields with real content**, where "real"
excludes the empty markers (`لا توجد`, `غير محدد`, …). Expected yield:
**74 of 97**.

Also carried over: the `EXCLUDE` name list. Contact forms, a staff social
club, a live-broadcast page and internal treasury operations are not
citizen transactions, and indexing them lets a vague ministry question land
on a contact form instead of a procedure.

Rejected services are **written to `data/needs_review/` with the reason** —
never dropped in silence.

### 4.3 Counts must reconcile or the build fails

The probe found 44 service headings but parsed 43, and lost one authority
to an anchoring assumption. Both losses were invisible.

So: the importer counts headings by a loose pattern, counts records
actually emitted, and **fails loudly on any discrepancy**. Import that
silently drops 1 of 44 services is the same class of bug as the
`service_id` mismatch — everything looks fine and one service is simply
never answerable.

---

## 5. What this costs us, honestly

| | Old (LLM restructure) | New (verbatim) |
|---|---|---|
| Invented facts | possible; validator caught 5/15 | structurally impossible |
| Detail loss | real — model summarised | none |
| Clean prose | yes | no; OCR artefacts ship |
| Citations | URL, when pypdf recovered one | **text reference only** |
| Country spelled correctly | yes | **no — `غُمان` ships** |
| LLM cost | ~1 request/service | zero |
| Services gained | 10 of 15 | ~74 of 97 |

The honest summary: **more coverage, zero fabrication, uglier text.**

The `غُمان` problem is worth one more sentence, because a judge may see it.
It appears in authority *headings*, which are front-matter, not body text —
so those specific strings can be taken from the registry's canonical
`name_ar` rather than from OCR. That is not a text fix; it is choosing a
different source for a field. Body text still ships as-is.

---

## 6. Decisions needed before I build

1. **Allow `source_ref` as an alternative to `source_url` in the loader?**
   Without this, nothing ships. (Recommend: yes — it finishes proposal row 6.)
2. **Bring 10 authorities into coverage** — 6 promoted from `not_covered`,
   4 added? This changes what Daleel refuses, in a demo. (Recommend: yes
   for the 6 promotions, which have real services; decide الدفاع and
   الإعلام separately — their content is thin.)
3. **Mechanical `{authority}-{NN}` service IDs?** (Recommend: yes.)
4. **Accept ~74 services with visible OCR artefacts into the demo corpus,
   or stage them behind a flag** and promote per-authority after a human
   reads them?

Question 4 is the real one. 74 services triples the corpus and every one is
`verified_by: PENDING` — the same status all 26 current files carry.

---

## 7. Scope

**In:** parser, cleaner, gate, emitter, `source_ref` loader change,
registry migration, reconciliation check, tests, retire
`import_pdf_corpus.py` — all done.

**Out:** running OCR on new PDFs (no PDFs in the repo; add later behind
`--pdf <path>` using the existing `chatbot.rag.ocr`), hand-written registry
`variants` for imported services, and correcting OCR errors.
