# data/eval — the D7 evaluation set

`eval_set.csv` is both the accuracy measurement and the **retrieval regression suite**.
Run it after every change to the threshold, the margin, the normalizer, the registry, or
the corpus.

> **Read [../../docs/D7_METHODOLOGY.md](../../docs/D7_METHODOLOGY.md) before adding
> rows.** The authorship rule there is what makes the resulting number defensible.

## The rule, in one line

**Whoever writes the questions must not have read `data/registry/authorities.json`.**

Otherwise the figure measures whether we listed what we listed.

## The six `SEED-*` rows

They exist to show the column format and nothing else. They were written by someone
(Claude) who wrote the registry itself, so they are contaminated by definition. Their
`author` field starts with `REGISTRY-AWARE`, and `scripts/run_eval.py` **excludes any
such row from the reported figures**.

Replace them. They are not a starting corpus of questions.

## Columns

| Column | Filled by | Notes |
|---|---|---|
| `id` | author | any stable id |
| `question_ar` | author | as a citizen would really type it, misspellings included |
| `question_type` | author | `in_corpus`, `ambiguous`, `unknown_service`, `uncovered_authority`, `out_of_domain` |
| `expected_gate` | author | `answer`, `clarify`, `refuse_uncovered`, `refuse_no_content` |
| `expected_service_id` | author | required when `expected_gate` is `answer` |
| `expected_answer` | reviewer | human review only — **not** part of the automated figure |
| `expected_source_url` | reviewer | human review only |
| `author` | author | your name. `REGISTRY-AWARE-*` excludes the row |
| `notes` | anyone | |

There is deliberately **no** "is this phrasing in the registry?" column. The harness
computes that by running `registry.classify()`, because a human marking it would be
making exactly the judgement the methodology removes.

## Running

```bash
python scripts/run_eval.py
```
