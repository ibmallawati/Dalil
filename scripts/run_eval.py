"""
D7 evaluation harness — measures the GATE, and reports two numbers.

    python scripts/run_eval.py

See docs/D7_METHODOLOGY.md for why it works this way. The short version:

  - Accuracy is measured on the routing DECISION (answer / clarify /
    refuse), not on prose similarity to a reference answer. An LLM's
    wording will never string-match a reference, and scoring it that way
    punishes correct answers for being phrased differently.

  - Two figures are reported. OVERALL is the headline. FALL-THROUGH —
    restricted to questions where L1 resolved nothing — is the real
    result, because that is the path where retrieval, the threshold, the
    margin check and the grounding check actually have to work. When L1
    resolves a service, the answer comes from deterministic lookup and the
    measurement mostly reflects the registry's own coverage.

  - The fall-through split is COMPUTED here, by running the registry, not
    read from a human-marked column. A person marking it would be making
    exactly the judgement the methodology exists to remove.

  - Rows authored by someone who had read the registry are EXCLUDED.
    Their author field starts with REGISTRY-AWARE.

This does NOT call the generation model. It measures routing and
grounding, which is what determines whether Daleel answers the right
question from the right source. Answer-text quality is a human review
column.
"""

import argparse
import csv
import io
import sys
from collections import Counter
from pathlib import Path

from chatbot.agent import registry
from chatbot.agent.router import Strategy, plan_and_gather
from chatbot.config import settings
from chatbot.logging_config import configure_logging

# Windows consoles default to cp1252, which cannot encode Arabic.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CONTAMINATED_PREFIX = "REGISTRY-AWARE"

GATE_TO_STRATEGY = {
    "answer": (Strategy.WHOLE_SERVICE, Strategy.SIMILARITY),
    "clarify": (Strategy.CLARIFY,),
    "refuse_uncovered": (Strategy.REFUSE_UNCOVERED,),
    "refuse_no_content": (Strategy.REFUSE_NO_CONTENT,),
}


def score_row(row):
    """Return (ok, actual_strategy, detail)."""
    question = row["question_ar"].strip()
    expected_gate = row["expected_gate"].strip()

    plan = plan_and_gather(question)
    allowed = GATE_TO_STRATEGY.get(expected_gate)
    if allowed is None:
        return False, plan.strategy, f"unknown expected_gate {expected_gate!r}"

    if plan.strategy not in allowed:
        return False, plan.strategy, f"expected {expected_gate}"

    # For answer rows, being right about the GATE is not enough -- the
    # grounding has to come from the right service, or Daleel answered a
    # different question confidently.
    if expected_gate == "answer":
        want = row["expected_service_id"].strip()
        if not want:
            return False, plan.strategy, "expected_service_id missing from the row"
        got = {c.get("service_id") for c in plan.chunks}
        if want not in got:
            return False, plan.strategy, f"grounded in {sorted(got)}, expected {want}"

    return True, plan.strategy, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-file", default=None)
    parser.add_argument("--show-passes", action="store_true")
    args = parser.parse_args()

    configure_logging()

    eval_file = Path(args.eval_file) if args.eval_file else settings.eval_file
    if not eval_file.exists():
        print(f"No eval set at {eval_file}. See docs/D7_METHODOLOGY.md.")
        return 1

    with open(eval_file, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    fixture_index = "fixtures" in str(settings.index_file).lower()

    eligible, excluded = [], []
    for row in rows:
        (excluded if row.get("author", "").strip().upper().startswith(CONTAMINATED_PREFIX)
         else eligible).append(row)

    print(f"eval set   : {eval_file}")
    print(f"index      : {settings.index_file.parent}")
    print(f"threshold  : {settings.similarity_threshold}   margin: {settings.ambiguity_margin}")
    print(f"rows       : {len(rows)}  eligible: {len(eligible)}  excluded: {len(excluded)}")
    if excluded:
        print(f"             ({len(excluded)} excluded as REGISTRY-AWARE — see docs/D7_METHODOLOGY.md)")
    print("=" * 74)

    if not eligible:
        print("\nNo eligible rows.")
        print("Every row was written by someone who had read the registry, so any")
        print("figure from this set would measure whether we listed what we listed.")
        print("Add rows authored registry-blind. See docs/D7_METHODOLOGY.md §2.")
        return 1

    results = []
    for row in eligible:
        ok, strategy, detail = score_row(row)
        # Computed, not declared: did the deterministic layer have ANY
        # opinion about this phrasing?
        #
        # Not `service_id is not None`. A registry-DECLARED ambiguity
        # («السجل التجاري» -> ask) leaves service_id empty, but the registry
        # plainly recognised the phrase — counting that as fall-through
        # would credit the hard lane with a decision the easy lane made.
        # Likewise an authority-only match, and an uncovered-authority
        # refusal: all three are the registry matching something someone
        # wrote down.
        resolved = registry.classify(row["question_ar"]).verdict is not registry.Verdict.UNKNOWN
        results.append((row, ok, strategy, detail, resolved))

        if not ok or args.show_passes:
            mark = "PASS" if ok else "FAIL"
            lane = "L1" if resolved else "fall-through"
            print(f"[{mark}] {row['id']:<10} {lane:<13} {row['question_ar']}")
            if detail:
                print(f"        got={strategy.value}  {detail}")

    overall = [r for r in results if r[1]]
    fall_through = [r for r in results if not r[4]]
    fall_through_ok = [r for r in fall_through if r[1]]

    print("=" * 74)
    print(f"OVERALL        {len(overall)}/{len(results)}"
          f"   ({100 * len(overall) / len(results):.0f}%)")
    if fall_through:
        print(f"FALL-THROUGH   {len(fall_through_ok)}/{len(fall_through)}"
              f"   ({100 * len(fall_through_ok) / len(fall_through):.0f}%)"
              f"   <-- the real result")
    else:
        print("FALL-THROUGH   no rows where L1 failed to resolve a service.")
        print("               Every question matched a phrase already in the registry,")
        print("               so this set cannot measure the fall-through path at all.")
        print("               Add shorter/vaguer phrasings. See D7_METHODOLOGY.md §5.")

    by_type = Counter(r[0].get("question_type", "?") for r in results if not r[1])
    if by_type:
        print("\nfailures by question_type:")
        for kind, n in by_type.most_common():
            print(f"  {n:3d}  {kind}")

    if fixture_index:
        print("\n" + "*" * 74)
        print("* FIXTURE INDEX — invented content. These numbers demonstrate that the")
        print("* harness runs. They are NOT results and must not be quoted.")
        print("*" * 74)

    return 0


if __name__ == "__main__":
    sys.exit(main())
