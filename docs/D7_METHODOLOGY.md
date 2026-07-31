# D7 — evaluation methodology

**This document exists so the accuracy figure means something when it is said out loud
to a panel.**

---

## 1. The problem this guards against

Daleel's first gate (L1) is a keyword registry: a hand-written list of authorities,
services, and their Arabic name variants.

If the evaluation questions are written by someone who has read that list, they will
naturally reach for the phrasings it contains. The resulting accuracy figure then
measures **whether we listed what we listed** — a tautology that scores near 100% and
proves nothing.

That is not a hypothetical. It is the default outcome, because the person most able to
write realistic Arabic government questions quickly is usually the person who just
finished writing the registry.

---

## 2. The authorship rule

> **Evaluation questions must be written by someone who has NOT read
> `data/registry/authorities.json`.**

Practically:

- The question author does not open the registry, the corpus files, or `CORPUS_SPEC.md`
  before writing.
- They write from how a real Omani citizen would ask — including short, vague, colloquial
  and misspelled phrasings.
- The `author` column records who wrote each row, so contamination is traceable rather
  than assumed away.

Rows whose `author` begins with `REGISTRY-AWARE` are **excluded from the reported
figures** by the harness. Any seed rows written by someone who had seen the registry are
marked this way and must be replaced.

---

## 3. Two numbers, and which one is real

The harness reports both:

| Number | What it covers | What it means |
|---|---|---|
| **Overall accuracy** | every eligible row | headline figure; flattering, because L1 resolves the easy ones deterministically |
| **Fall-through accuracy** | rows where **L1 had no opinion at all** | **the real result** |

"No opinion" means the registry returned `UNKNOWN` — it matched neither a service, nor
an authority, nor a declared ambiguity, nor a known-uncovered authority. All four of
those are the registry recognising a phrase somebody wrote down, and all four belong in
the easy lane. In particular a registry-declared ambiguity leaves `service_id` empty
while still being a fully deterministic decision, so "did it resolve a service?" is the
wrong test.

**The second number is the one to quote and defend.**

When L1 resolves a service, the answer is produced by deterministic lookup — the
registry matched a phrase someone wrote down, and the whole service file is loaded. That
path is reliable by construction, and measuring it mostly measures the registry's
coverage.

The fall-through path is where the system has to actually work: no keyword matched,
so retrieval, the similarity threshold, the margin check and the grounding self-check
all have to do their jobs. **That is the number that describes Daleel's behaviour on a
question nobody anticipated** — which is every question a judge invents on the spot.

### The split is computed, not declared

The harness runs `registry.classify()` on each question and derives whether L1 resolved
a service. A human marking this column would be making exactly the judgement the
methodology is designed to remove.

---

## 4. What each row asserts

Accuracy is measured on the **gate decision**, not on prose similarity:

| `expected_gate` | Meaning |
|---|---|
| `answer` | Daleel should answer, grounded in `expected_service_id` |
| `clarify` | Daleel should ask one clarifying question |
| `refuse_uncovered` | the question names an authority Daleel does not cover |
| `refuse_no_content` | in scope, but Daleel has no grounds to answer |

A row counts as correct when the router's strategy matches `expected_gate` **and**, for
`answer` rows, the grounding came from `expected_service_id`.

`expected_answer` and `expected_source_url` are for human review of answer quality. They
are not part of the automated figure — an LLM's prose will never string-match a
reference answer, and pretending otherwise produces a number that punishes correct
answers for being worded differently.

---

## 5. Composition

Aim for **30+ rows**, roughly:

| `question_type` | Target | Purpose |
|---|---|---|
| `in_corpus` | ~18 | normal citizen questions across all 6 services |
| `ambiguous` | ~4 | genuinely two services in play |
| `unknown_service` | ~4 | covered authority, service Daleel does not have |
| `uncovered_authority` | ~6 | a different Omani ministry |
| `out_of_domain` | ~4 | not government services at all |

The out-of-domain rows are what make the refusal figure meaningful. Without them,
"27/30" says nothing about whether Daleel refuses when it should.

Deliberately include **short and vague** phrasings («جواز», «تجديد», «رخصة»). Those are
how people actually type, and they are where the fall-through path gets exercised.

---

## 6. Running it

```bash
python scripts/run_eval.py
```

Against the real index. The harness **refuses to report a headline figure when running
against fixtures** — fixture content is invented, so a number measured against it is
meaningless while looking legitimate.

Re-run after every retrieval change. A change to the threshold, the margin, the
normalizer, the registry or the corpus can move these numbers, and the point of a
regression suite is to notice.

---

## 7. What not to do

- Do not tune the threshold or the margin **against the eval set and then report that
  same set's accuracy.** That is fitting to the test. If D2's tuning uses these
  questions, the reported figure needs a held-out subset.
- Do not quote a number measured against `data/fixtures/`.
- Do not add registry variants **because** an eval question failed to match. That is
  backwards: the eval exists to measure coverage, not to be satisfied by widening it
  until it passes. Fix it after measuring, and record both figures.
