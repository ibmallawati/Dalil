"""
router.py — chooses HOW to gather grounding, based on what L1 resolved.

This is the agentic decision that sits between "is this in scope?" (the
registry) and "here is an answer" (generation). It runs before any
embedding, and for most questions it means no embedding happens at all.

--- L1 is a HINT, not a gate ----------------------------------------------

L1 used to short-circuit retrieval: a resolved `service_id` loaded that
whole file and similarity never ran. That was defensible while the answer
was a fixed card — top-k could not fill eight fields, so a model would fill
them from general knowledge instead. The card is gone, and the bypass
outlived its reason.

What it cost, measured:

  «كم رسوم تجديد جواز السفر ورخصة السياقة؟»
    L1 keyword-matches «جواز السفر» first, loads the passport file, and
    stops. The driving-licence half of the question is silently dropped,
    and the answer looks confident anyway.

    Similarity ranks rop-driving-licence-renewal/الخطوات at 0.8607,
    rop-passport-renewal/الرسوم at 0.8566 and
    rop-driving-licence-renewal/الرسوم at 0.8515 — both services, no
    keyword needed.

Retrieval now always runs. When L1 did resolve a service, its sections are
merged in so they are guaranteed present — L1 contributes recall it is good
at (exact Arabic service names) without being able to suppress anything.

--- The strategies -------------------------------------------------------

REFUSE_UNCOVERED   L1 named a different, known Omani authority.
                   No embedding. D6 refusal path 2.

CLARIFY            Two candidate services — declared by the registry, or
                   detected from a near-tie in retrieval scores. No LLM
                   call, so the clarification cannot rate-limit mid-demo.

SIMILARITY         The main road. Retrieval, plus L1's hinted service if it
                   had one.

WHOLE_SERVICE      Only where the service is not in question: the citizen
                   tapped a clarifying option, or the clarify budget is
                   spent and every candidate gets loaded.

REFUSE_NO_CONTENT  Nothing was retrieved at all.
"""

import logging
from dataclasses import dataclass, field, replace
from enum import Enum

from chatbot.agent import registry
from chatbot.agent.registry import RegistryMatch, Verdict
from chatbot.config import settings
from chatbot.rag.retrieval import fetch_service, retrieve

logger = logging.getLogger(__name__)

# Retrieval is now the MAIN path, not a fallback, so k is wider than the
# old 3: a question can legitimately span two services, and the margin check
# needs enough candidates to see a rival service at all.
SIMILARITY_K = 8

# Ceiling on what reaches the model. Similarity breadth plus a hinted
# service's depth can add up, and an unbounded context is how a long answer
# quietly becomes a truncated one — reasoning tokens are billed against the
# same budget as the reply (see chatbot.llm).
MAX_CONTEXT_CHUNKS = 12


class Strategy(Enum):
    REFUSE_UNCOVERED = "refuse_uncovered"
    REFUSE_NO_CONTENT = "refuse_no_content"
    CLARIFY = "clarify"
    WHOLE_SERVICE = "whole_service"
    SIMILARITY = "similarity"


@dataclass(frozen=True)
class RetrievalPlan:
    strategy: Strategy
    match: RegistryMatch
    chunks: tuple = field(default_factory=tuple)

    @property
    def is_refusal(self) -> bool:
        return self.strategy in (Strategy.REFUSE_UNCOVERED, Strategy.REFUSE_NO_CONTENT)

    @property
    def grounded(self) -> bool:
        return bool(self.chunks)


def plan_and_gather(question: str, *, service_id=None, allow_clarify=True) -> RetrievalPlan:
    """
    Run L1, pick a strategy, and gather grounding accordingly.

    Returns everything the generation stage needs, plus the reason it was
    chosen — so a log line explains not just what Daleel answered but why
    it looked where it looked.

    `service_id` — the citizen tapped a clarifying option. Their choice is
    an answer, not a hint, so it goes straight to that service and no
    further classification runs.

    `allow_clarify` — D4 permits at most ONE clarifying turn. This is
    enforced HERE rather than in the browser: an agent that interrogates a
    judge for three rounds reads as broken, and a rule that lives only in
    frontend state is one refresh away from not existing.
    """
    if service_id:
        chunks = fetch_service(service_id)
        logger.info("route_clarified_choice",
                    extra={"service_id": service_id, "sections": len(chunks)})
        match = registry.classify(question)
        if not chunks:
            return RetrievalPlan(Strategy.REFUSE_NO_CONTENT, match)
        return RetrievalPlan(Strategy.WHOLE_SERVICE, match, tuple(chunks))

    match = registry.classify(question)

    # 1. A different authority, and nothing covered alongside it.
    if match.verdict is Verdict.UNCOVERED_AUTHORITY:
        logger.info("route_refuse_uncovered",
                    extra={"authority": match.authority_en, "matched_on": match.matched_on})
        return RetrievalPlan(Strategy.REFUSE_UNCOVERED, match)

    # 2. Genuinely two services in play. Ask, don't guess — and do it
    #    without an LLM call, so it cannot rate-limit mid-demo.
    if match.needs_clarification:
        if not allow_clarify:
            # One clarifying turn has already been spent, and asking again
            # is the failure D4's limit exists to prevent.
            #
            # Load EVERY candidate service rather than picking one. Picking
            # arbitrarily gets it right half the time, and when it is wrong
            # L3 correctly reports it cannot answer — so the citizen gets a
            # refusal for a question Daleel could have answered from the
            # other candidate. Giving the model both lets it answer from
            # whichever actually matches.
            chunks = []
            for candidate in match.candidate_service_ids:
                chunks.extend(fetch_service(candidate))
            logger.info("route_clarify_budget_spent",
                        extra={"candidates": list(match.candidate_service_ids),
                               "sections": len(chunks)})
            if chunks:
                return RetrievalPlan(Strategy.WHOLE_SERVICE, match, tuple(chunks))
            return RetrievalPlan(Strategy.REFUSE_NO_CONTENT, match)
        logger.info("route_clarify",
                    extra={"candidates": list(match.candidate_service_ids),
                           "matched_on": match.matched_on})
        return RetrievalPlan(Strategy.CLARIFY, match)

    # 3. Everything else: RETRIEVAL ALWAYS RUNS.
    #
    # L1 used to short-circuit here — a resolved service_id loaded that whole
    # file and similarity never ran. Measured cost of that:
    #
    #   «كم رسوم تجديد جواز السفر ورخصة السياقة؟»
    #     old: whole_service -> passport only; the driving-licence half of
    #          the question was silently dropped and the answer looked
    #          confident anyway.
    #     now: similarity surfaces BOTH fee sections (0.8566 and 0.8515),
    #          and the answer can cover both.
    #
    # So the resolved service becomes a HINT that guarantees its sections
    # are present, not a decision that suppresses everything else.
    chunks = retrieve(question, k=SIMILARITY_K)
    hinted = _merge_hinted_service(chunks, match.service_id)

    # Registry/corpus drift: L1 named a service the corpus does not provide.
    # Retrieval may still carry the answer, so this is not fatal — but it is
    # a data-quality bug, and it goes unnoticed forever if nothing says so.
    if match.service_id and not any(
            c.get("service_id") == match.service_id for c in hinted):
        logger.warning("route_service_has_no_content",
                       extra={"service_id": match.service_id,
                              "hint": "registry covers a service the corpus does not provide"})

    if not hinted:
        return RetrievalPlan(Strategy.REFUSE_NO_CONTENT, match)

    logger.info("route_retrieved", extra={
        "verdict": match.verdict.value,
        "l1_service": match.service_id,
        "chunks": len(hinted),
        "services": sorted({c.get("service_id") for c in hinted if c.get("service_id")}),
        "top_score": round(chunks[0]["score"], 4) if chunks else None,
    })
    chunks = hinted

    # Undeclared ambiguity: two different services scoring near-identically.
    #
    # Suppressed when L1 resolved a service, because then the ambiguity is
    # not undeclared — L1 already broke the tie, deterministically, on the
    # words the citizen actually wrote. Observed when the demotion first
    # landed: «كيف أجدد جواز سفري؟» retrieved
    # mht-tour-guide-licence-renewal at 0.8636 against
    # rop-passport-renewal at 0.8611, and Daleel asked whether the citizen
    # meant a tour-guide licence. e5 scores «تجديد» heavily and the 0.0025
    # separating those two is noise; «جواز سفري» is not.
    #
    # This is the boundary of the demotion: L1 no longer suppresses
    # RETRIEVAL, but it still outranks a similarity tie-break.
    ambiguous = None if match.service_id else _detect_margin_ambiguity(chunks)
    if ambiguous:
        logger.info("route_clarify_by_margin",
                    extra={"candidates": [o["service_id"] for o in ambiguous["options"]],
                           "margin": ambiguous["margin"]})
        return RetrievalPlan(
            Strategy.CLARIFY,
            replace(
                match,
                candidate_service_ids=tuple(o["service_id"] for o in ambiguous["options"]),
                clarify_question_ar=ambiguous["question_ar"],
                clarify_options=tuple(ambiguous["options"]),
            ),
        )

    return RetrievalPlan(Strategy.SIMILARITY, match, tuple(chunks))


def _merge_hinted_service(chunks, service_id):
    """
    Similarity results, with the L1-resolved service guaranteed present.

    Two failure modes this sits between:

      - L1 bypassing retrieval entirely (the old behaviour) drops every
        other service a question touches;
      - pure similarity can rank one section of the right service highly
        while leaving the rest of that service out, so «ما إجراءات تجديد
        الجواز؟» might retrieve the fee section and not the steps.

    Merging fixes both: similarity decides breadth, the L1 hint guarantees
    depth on the service it is confident about. Retrieval order is kept, so
    the best-matching chunk stays primary and drives the citation heading.
    """
    merged = list(chunks)
    if not service_id:
        return merged[:MAX_CONTEXT_CHUNKS]

    seen = {(c.get("service_id"), c.get("section")) for c in merged}
    for chunk in fetch_service(service_id):
        key = (chunk.get("service_id"), chunk.get("section"))
        if key not in seen:
            seen.add(key)
            merged.append(chunk)
    return merged[:MAX_CONTEXT_CHUNKS]


def _detect_margin_ambiguity(chunks):
    """
    Two different services, near-identical scores -> ask instead of guessing.

    Taking top-1 silently is the failure this prevents. When «تجديد الرخصة»
    scores 0.841 against the trade licence and 0.838 against the driving
    licence, the ranking is noise, not a decision — and answering from the
    wrong service produces a confidently wrong procedure with a real
    citation attached.

    Compares top-1 against the best chunk of the FIRST DIFFERENT service
    rather than literal top-2, because top-2 is usually another section of
    the same service, which is agreement rather than ambiguity.

    Registry-declared ambiguity is handled earlier and more precisely; this
    is the net for the kind nobody has written down yet.
    """
    top = chunks[0]
    top_service = top.get("service_id")
    if not top_service or top.get("score") is None:
        return None

    rival = next(
        (c for c in chunks[1:]
         if c.get("service_id") and c["service_id"] != top_service
         and c.get("score") is not None),
        None,
    )
    if rival is None:
        return None

    margin = top["score"] - rival["score"]
    if margin >= settings.ambiguity_margin:
        return None

    options = [
        {"label_ar": top.get("service") or top_service, "service_id": top_service},
        {"label_ar": rival.get("service") or rival["service_id"],
         "service_id": rival["service_id"]},
    ]
    return {
        "margin": round(margin, 4),
        "question_ar": (
            f"سؤالك قد يتعلق بأكثر من خدمة. "
            f"هل تقصد «{options[0]['label_ar']}» أم «{options[1]['label_ar']}»؟"
        ),
        "options": options,
    }
