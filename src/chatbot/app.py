"""
Daleel — Arabic government-services assistant for Omani citizens.

Request path:

    question
      -> L1  registry pre-gate      (deterministic, no network)
      -> retrieval strategy         (whole service / similarity / clarify / refuse)
      -> D5 + L3 generation         (one LLM call, card + confidence)
      -> structured response

Every stage runs through the Phase 5 run_stage() wrapper, so a broken
request says WHICH stage broke, how many attempts it took, and how long it
ran. One JSON line per stage goes to stdout.

--- What is NOT here anymore --------------------------------------------

The Phase 4 text-to-SQL branch is no longer in the request path. It queried
Omantel customer databases; Daleel has no database and no use for one. The
code remains in chatbot.sql and its tests still run — it is part of this
project's history and may be discussed with judges — but the router is now
a clean decision about WHERE to ground an answer, not WHICH backend to use.
"""

import csv
import logging
import time
from datetime import datetime

from flask import Flask, jsonify, render_template, request

from chatbot.config import BASE_DIR, settings
from chatbot.logging_config import configure_logging

# Configure logging BEFORE importing anything that logs at import time
# (retrieval loads its index at import; the registry loads its coverage map).
configure_logging()
logger = logging.getLogger(__name__)

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from chatbot.agent import registry
from chatbot.agent.answer import generate_answer
from chatbot.agent.router import Strategy, plan_and_gather
from chatbot.reliability.run_record import RunRecord
from chatbot.reliability.stage_runner import (
    API_POLICY,
    DEFAULT_POLICY,
    StageStatus,
    run_stage,
)

LOG_FILE = settings.chat_log_file
if not LOG_FILE.exists():
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["date", "question", "strategy", "answer_time_sec"])

STAGE_LOG_FILE = settings.stage_log_file

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

limiter = Limiter(key_func=get_remote_address, app=app,
                  default_limits=["100 per hour"])


def log_exchange(question, strategy, answer_time):
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            question, strategy, round(answer_time, 2),
        ])


def latest_user_message(history):
    for turn in reversed(history):
        if turn.get("role") == "user":
            return turn.get("content", "")
    return ""


def build_response(plan, question, history):
    """
    Turn a retrieval plan into the payload the UI renders.

    Four shapes, and the UI treats them differently on purpose — a refusal
    must not look like an answer with empty fields.
    """
    if plan.strategy is Strategy.REFUSE_UNCOVERED:
        return {
            "format": "refusal",
            "reply": registry.uncovered_authority_message(plan.match),
            "reason": "uncovered_authority",
        }

    if plan.strategy is Strategy.CLARIFY:
        return {
            "format": "clarify",
            "reply": plan.match.clarify_question_ar,
            # Tappable buttons. Labels and service_ids come from the
            # registry, so no LLM call is involved and the question is
            # identical on every run.
            "options": [dict(o) for o in plan.match.clarify_options],
        }

    if plan.strategy is Strategy.REFUSE_NO_CONTENT:
        return {
            "format": "refusal",
            "reply": registry.unknown_service_message(plan.match),
            "reason": "no_grounding",
        }

    # Grounded: one LLM call produces the answer and its own confidence.
    answer = generate_answer(question, list(plan.chunks), history=history)

    # L3: the model reported the retrieved text does not answer this. Render
    # the refusal — an answer the model itself disowns should not be dressed
    # up as one, and the refusal names the authority and its portal.
    if answer.get("confidence") == "none":
        return {
            "format": "refusal",
            "reply": registry.unknown_service_message(plan.match),
            "reason": "ungrounded",
            "served_by": answer.get("_served_by"),
        }

    payload = {k: v for k, v in answer.items() if k != "_served_by"}
    payload["served_by"] = answer.get("_served_by")
    return payload


@app.route("/")
def home():
    """
    The homepage carries the browse section, so the coverage map ships with
    the HTML instead of arriving over a second request.

    No new endpoint for it on purpose: the directory is derived entirely
    from data/registry/authorities.json, which is loaded and precomputed at
    import. There is nothing to fetch that the page does not already know.
    """
    return render_template("index.html", directory=registry.browse_directory())


@app.route("/healthz")
@limiter.exempt
def healthz():
    """
    Liveness probe that reports what actually loaded, not just that Flask
    is up. The coverage summary doubles as the scope statement: N
    authorities, M services, and adding one is a data operation.
    """
    from chatbot.llm import available_models
    from chatbot.rag.retrieval import KNOWLEDGE

    return jsonify({
        "status": "ok",
        "chunks_loaded": len(KNOWLEDGE),
        "coverage": registry.coverage_summary(),
        "models_available": available_models(),
    })


@app.route("/chat", methods=["POST"])
@limiter.limit("20 per minute")
def chat():
    data = request.get_json() or {}
    history = data.get("history", [])
    # Set when the citizen tapped a clarifying option. Their choice is an
    # answer, not a hint.
    service_id = data.get("service_id")
    # D4 allows at most ONE clarifying turn. Enforced server-side: a rule
    # living only in frontend state is one refresh away from not existing.
    clarify_count = int(data.get("clarify_count", 0))

    question = latest_user_message(history)
    if not question:
        return jsonify({"format": "refusal", "reply": "لم يصل أي سؤال."}), 400

    start = time.perf_counter()
    run = RunRecord(query=question, log_path=STAGE_LOG_FILE)
    logger.info("chat_request_received",
                extra={"request_id": run.request_id, "query": question[:200],
                       "service_id": service_id, "clarify_count": clarify_count})

    try:
        # ---- stage: route -------------------------------------------------
        # DEFAULT_POLICY: L1 and retrieval are local. Nothing here can
        # flake, so there is nothing worth retrying.
        r_route = run_stage(
            "route",
            # history[:-1] excludes the current question, which is passed
            # separately — including it would have the router classify the
            # same text twice.
            lambda: plan_and_gather(question, service_id=service_id,
                                    allow_clarify=clarify_count < 1,
                                    history=history[:-1]),
            policy=DEFAULT_POLICY,
        )
        run.add(r_route, extra={})
        if r_route.status is StageStatus.FAILED:
            raise RuntimeError(f"route stage failed: {r_route.error}")
        plan = r_route.payload

        # ---- stage: generate ----------------------------------------------
        # API_POLICY: the only network call in the request. Refusals and
        # clarifications never reach the model at all.
        r_gen = run_stage(
            "generate",
            lambda: build_response(plan, question, history[:-1]),
            policy=API_POLICY,
        )
        run.add(r_gen, extra={"strategy": plan.strategy.value,
                              "chunk_ids": [c.get("chunk_id") for c in plan.chunks]})
        if r_gen.status is StageStatus.FAILED:
            raise RuntimeError(f"generate stage failed: {r_gen.error}")

        payload = r_gen.payload
        answer_time = time.perf_counter() - start
        log_exchange(question, plan.strategy.value, answer_time)

        logger.info("chat_request_completed", extra={
            "request_id": run.request_id,
            "strategy": plan.strategy.value,
            "format": payload.get("format"),
            "confidence": payload.get("confidence"),
            "served_by": payload.get("served_by"),
            "total_duration_s": round(answer_time, 2),
        })

        payload.update({
            "strategy": plan.strategy.value,
            "answer_time": round(answer_time, 2),
        })
        return jsonify(payload)

    except Exception as error:
        logger.error("chat_request_failed",
                     extra={"request_id": run.request_id,
                            "failed_stage": run.failed_stage()},
                     exc_info=True)
        # Arabic, and honest about being a failure rather than an answer.
        return jsonify({
            "format": "refusal",
            "reason": "error",
            "reply": "تعذّر إنتاج إجابة الآن. يرجى المحاولة مرة أخرى.",
            "detail": str(error)[:200],
        }), 500

    finally:
        run.write()


@app.errorhandler(429)
def ratelimit_handler(error):
    logger.warning("rate_limit_exceeded",
                   extra={"remote_addr": get_remote_address(), "detail": str(error)})
    return jsonify({
        "format": "refusal",
        "reply": "عدد كبير من الطلبات. يرجى الانتظار قليلاً ثم المحاولة مجدداً.",
    }), 429


def warm_embedder():
    """
    Load the embedding model at startup instead of on first use.

    sentence-transformers loads lazily, so without this the FIRST question
    that falls through to similarity search pays the load cost. Measured:
    45 seconds, arriving mid-conversation on the sixth request, long after
    the app looked ready. Every question that resolved a service ran fast
    and gave no warning it was coming.

    Paying it at boot is strictly better: the delay happens once, before
    anyone is watching, and `/healthz` does not go green until it is done.
    """
    from chatbot.rag.retrieval import get_model

    start = time.perf_counter()
    logger.info("embedder_warmup_started", extra={"model": settings.embed_model})
    get_model()
    logger.info("embedder_warmup_complete",
                extra={"model": settings.embed_model,
                       "duration_s": round(time.perf_counter() - start, 1)})


def main():
    warm_embedder()
    app.run(host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
