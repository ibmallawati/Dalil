"""
router.py — chooses HOW to gather grounding, based on what L1 resolved.

This is the agentic decision that sits between "is this in scope?" (the
registry) and "here is an answer" (generation). It runs before any
embedding, and for most questions it means no embedding happens at all.

--- Why retrieval strategy depends on L1 ---------------------------------

The registry does not merely recognise an authority; when it matches a
service variant it resolves a specific `service_id`. Once that is known,
similarity search is actively harmful:

  D5's answer card has eight fields. Top-k retrieval over a seven-section
  service can only ever supply a few of them. Faced with a card it cannot
  fill from the context, a model does one of two things — null the fields,
  or fill them from general knowledge. The second produces an invented fee
  or an invented document list underneath a real government source URL.
  That is the single worst output this system can produce, and top-k over
  a resolved service is how you get there by default.

A curated service file is 20-40 lines. There is nothing to select from.

--- The four strategies --------------------------------------------------

REFUSE_UNCOVERED   L1 named a different, known Omani authority.
                   No embedding. D6 refusal path 2.

CLARIFY            L1 resolved two or more candidate services.
                   No embedding, no LLM call — the question and its options
                   come from the registry, so the clarification is
                   deterministic and fires identically on every run.

WHOLE_SERVICE      L1 resolved exactly one service. Load all its sections.
                   No similarity, no threshold.

SIMILARITY         L1 matched an authority only, or had no opinion at all.
                   Embed and search, as before. This is the ONLY path where
                   the D2 threshold is consulted.

Note what that last line means: the narrow (~0.015) similarity separation
measured on e5 only ever governs questions the deterministic layer could
not resolve. It is a fallback, not the main road.
"""

import logging
from dataclasses import dataclass, field, replace
from enum import Enum

from chatbot.agent import registry
from chatbot.agent.registry import RegistryMatch, Verdict
from chatbot.config import settings
from chatbot.rag.retrieval import fetch_service, retrieve

logger = logging.getLogger(__name__)

# Wider than the old k=3. Similarity is now the fallback path rather than
# the main road, and the margin check needs enough candidates to see a
# rival service at all — with k=3 over a 6-section service, every hit can
# easily belong to the same service and the ambiguity check never fires.
SIMILARITY_K = 5


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

    # 3. Exactly one service resolved -> load the whole thing.
    if match.service_id:
        chunks = fetch_service(match.service_id)
        if not chunks:
            # The registry claims coverage but the corpus has no file for
            # it. That is D6 refusal path 1 — a known authority, a service
            # we cannot actually document — and it is worth logging loudly
            # because it means the registry and the corpus disagree.
            logger.warning("route_service_has_no_content",
                           extra={"service_id": match.service_id,
                                  "hint": "registry covers a service the corpus does not provide"})
            return RetrievalPlan(Strategy.REFUSE_NO_CONTENT, match)
        logger.info("route_whole_service",
                    extra={"service_id": match.service_id, "sections": len(chunks)})
        return RetrievalPlan(Strategy.WHOLE_SERVICE, match, tuple(chunks))

    # 4. Authority-only, or no opinion. Fall back to similarity — the only
    #    path where the D2 threshold applies.
    chunks = retrieve(question, k=SIMILARITY_K)
    logger.info("route_similarity",
                extra={"verdict": match.verdict.value, "hits": len(chunks),
                       "top_score": round(chunks[0]["score"], 4) if chunks else None})
    if not chunks:
        return RetrievalPlan(Strategy.REFUSE_NO_CONTENT, match)

    # Undeclared ambiguity: two different services scoring near-identically.
    ambiguous = _detect_margin_ambiguity(chunks)
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
