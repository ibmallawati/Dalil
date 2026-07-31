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

## 7. Quota budget

**The default eval run costs ZERO LLM calls.**

`run_eval.py` measures the *gate decision* — did Daleel answer, clarify, or refuse, and
did it ground in the right service. All of that is the registry, the embedder (local),
and the router. None of it touches the generation API.

The same is true of the D3 normalization A/B (`fold_orthography` on vs off): it changes
what gets embedded, and embedding is local. **Re-running the whole 30-question set both
ways costs nothing.**

Quota is only consumed by `--with-answers`, which additionally generates each answer to
check `confidence` and field grounding.

| Run | LLM calls | Notes |
|---|---|---|
| `run_eval.py` | **0** | gate + grounding. Run this freely, as often as you like |
| `run_eval.py --with-answers` | 1 per row | ~30 calls; also records which model served each row |
| D3 fold A/B | **0** | retrieval only, both directions |

### Spending `--with-answers` safely

Free-tier quotas are **per model**, so the failover chain is also a budget pool. A
30-row run at ~30 calls fits comfortably in one model's daily allowance for every model
we have measured — but the ceilings are unpublished (see §8), so treat this as a budget
to plan rather than a certainty.

Rules:

1. **Never run `--with-answers` on the demo model.** Pin it to a fallback:
   `python scripts/run_eval.py --with-answers --model gemini-3.1-flash-lite`
2. **Check the per-model call counts** the harness prints at the end. If one model
   served every row, the chain never engaged and you spent one model's whole allowance.
3. **The `served_by` column matters.** An accuracy figure mixing three models is three
   different systems averaged together. Report which model produced the number, or
   pin one model for the run you intend to quote.
4. Daily quotas reset at **midnight Pacific = 11:00 Muscat**. A run in the Muscat
   morning spends *yesterday's* remaining quota.

---

## 8. What is not measured, and cannot be from here

Free-tier per-model quotas are **not published** in Google's documentation — the docs
say only that limits "can be viewed in Google AI Studio", which is authenticated and
per-project.

The only way to observe a ceiling from code is to hit it, and hitting it spends exactly
the quota being protected. So the number is knowable only from
**<https://aistudio.google.com/rate-limit>**, signed in as the key's owner.

The one figure observed directly is `gemini-3.5-flash`: `quotaValue: '20'`,
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` — read from the 429 body after
exhausting it. That model is excluded from the failover chain for that reason.

Before the evaluation, record from AI Studio, for every model in the chain: RPM, RPD,
TPM. Without RPD in particular, the demo's rehearsal budget is unknown.

---

## 9. What not to do

- Do not tune the threshold or the margin **against the eval set and then report that
  same set's accuracy.** That is fitting to the test. If D2's tuning uses these
  questions, the reported figure needs a held-out subset.
- Do not quote a number measured against `data/fixtures/`.
- Do not add registry variants **because** an eval question failed to match. That is
  backwards: the eval exists to measure coverage, not to be satisfied by widening it
  until it passes. Fix it after measuring, and record both figures.
