"""
Chatbot backend - Phase 4 (routing + MULTI-database text-to-SQL)
              + Phase 5 (fault tolerance: retry + stage-level structured log).

Flow:
- route_question() classifies each message as SQL or RAG.
- The SQL path spans MANY SQLite files (Layer A): each is ATTACHed
  read-only under an alias, so one query can ride through all of them.
- Adding a database = append a path to DB_PATHS and restart (like adding a PDF).
- Safe default: uncertain or SQL-unavailable falls back to RAG.

Phase 5 addition:
- Every stage (route / retrieve / generate / sql) now runs through
  run_stage(), which retries transient failures with backoff and reports
  exactly what happened. One JSONL line per stage is appended to
  stage_log.jsonl, so a broken request tells you WHICH stage broke,
  how many attempts it took, and how long it ran.
- The user-facing response (reply / route / answer_time) is UNCHANGED.
  The fault-tolerance layer is a side effect that runs alongside it.
"""

import csv
import logging
import os
import time
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from openai import OpenAI

from chatbot.logging_config import configure_logging
from chatbot.config import settings, BASE_DIR

# Configure logging BEFORE importing anything that logs at import time
# (chatbot.rag.retrieval loads its embedding index at import time and logs
# the outcome) - otherwise those lines bypass our JSON stdout handler and
# fall back to Python's unformatted default, or are dropped below WARNING.
configure_logging()
logger = logging.getLogger(__name__)

from chatbot.rag.retrieval import retrieve
from chatbot.sql.multi_db import answer_question_multi, build_schema_text_multi

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# --- Phase 5: fault tolerance layer ---------------------------------------
from chatbot.reliability.stage_runner import run_stage, StageStatus, DEFAULT_POLICY, API_POLICY
from chatbot.reliability.run_record import RunRecord

# Provider-neutral generation client. Gemini, Ollama and OpenAI all speak
# this same chat-completions shape, so which one answers is decided by
# .env (LLM_BASE_URL / LLM_MODEL), never by editing this file.
MODEL_ID = settings.llm_model
client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)

# Skip any file that isn't there, so one missing DB doesn't disable them all.
missing = [p for p in settings.db_paths if not os.path.exists(p)]
for p in missing:
    logger.warning("database_not_found", extra={"db_path": p})
DB_PATHS = [p for p in settings.db_paths if os.path.exists(p)]

try:
    SCHEMA_TEXT = build_schema_text_multi(DB_PATHS) if DB_PATHS else None
    if SCHEMA_TEXT:
        logger.info(
            "text_to_sql_ready",
            extra={"db_count": len(DB_PATHS), "db_paths": DB_PATHS},
        )
    else:
        logger.warning("text_to_sql_disabled", extra={"reason": "no_database_files"})
except Exception as e:
    SCHEMA_TEXT = None
    logger.error(
        "text_to_sql_disabled",
        extra={"reason": "schema_build_failed", "error": str(e)},
    )

SYSTEM_PROMPT = (
    "You are Omantel's customer service assistant. You are friendly, clear, "
    "and concise. You help customers with questions about Omantel plans, "
    "services, billing, and support."
)

# Header includes 'route'. Delete/rename an old 3-column chat_log.csv once.
LOG_FILE = settings.chat_log_file
if not LOG_FILE.exists():
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["date", "question", "route", "answer_time_sec"])

# Phase 5: separate structured per-stage log (JSON Lines, one line per stage).
STAGE_LOG_FILE = settings.stage_log_file

# templates/ and static/ live at the repo root, not next to this module (this
# file is src/chatbot/app.py) - Flask's default root_path would look for them
# next to the module, so point it at the repo root explicitly.
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

limiter = Limiter(
    key_func=get_remote_address,   # identify each visitor by their IP
    app=app,
    default_limits=["100 per hour"],  # a loose overall safety net
)


def log_exchange(question: str, route: str, answer_time: float) -> None:
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            question, route, round(answer_time, 2),
        ])


def latest_user_message(history):
    for turn in reversed(history):
        if turn.get("role") == "user":
            return turn.get("content", "")
    return ""


def route_question(question, client, model):
    """Decide which path answers this question. Defaults to RAG when unsure."""
    prompt = (
        "Classify the question. Reply with ONE word only:\n"
        "SQL = an exact fact or number from a table (price, count, list).\n"
        "RAG = an explanation, policy, or how-to from documents.\n\n"
        f"Question: {question}\nAnswer:"
    )
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], temperature=0)
    answer = resp.choices[0].message.content.strip().lower()
    return "sql" if "sql" in answer else "rag"


# --- Phase 5: answer_with_rag split into two stages ------------------------
# Previously one function did retrieve() AND the generate call together,
# which meant a failure could only ever be logged as "rag broke" with no
# way to tell which half. Splitting it lets the log say "retrieve" or
# "generate" specifically.

def build_rag_system_prompt(snippets):
    """Pure text assembly - no I/O, so it can't fail in a way worth wrapping."""
    system_content = SYSTEM_PROMPT
    if snippets:
        context = "\n".join(
            f"- (source: {s['source']}, page {s['page']}, chunk_id: {s['chunk_id']}) {s['text']}"
            for s in snippets
        )
        system_content += (
            "\n\nAnswer the user's question using ONLY the Omantel information "
            "below. Each item is labelled with its document and page. When you "
            "answer, briefly cite the source document and page you used.\n\n"
            f"Omantel information:\n{context}"
        )
    else:
        system_content += (
            "\n\nYou have no relevant Omantel information for this question. "
            "Tell the customer you don't have that information."
        )
    return system_content


def generate_rag_answer(user_message, history, snippets):
    """The 'generate' stage: one LLM call, given whatever retrieve() found."""
    system_content = build_rag_system_prompt(snippets)
    messages = [{"role": "system", "content": system_content}] + history
    completion = client.chat.completions.create(
        model=MODEL_ID, messages=messages, max_tokens=500, temperature=0.7)
    return completion.choices[0].message.content


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/healthz")
@limiter.exempt  # a deploy script / uptime monitor may poll this often
def healthz():
    """
    Liveness/readiness probe for the deploy script's smoke test (and a good
    candidate for Render's dashboard "Health Check Path" setting). Reports
    whether the optional subsystems actually loaded, not just that Flask
    is up - a 200 here with text_to_sql_enabled: false means the process
    is alive but a database went missing, which is worth knowing.
    """
    return jsonify({
        "status": "ok",
        "text_to_sql_enabled": SCHEMA_TEXT is not None,
        "db_count": len(DB_PATHS),
    })


@app.route("/chat", methods=["POST"])
@limiter.limit("20 per minute")  # limit each IP to 20 requests per minute
def chat():
    data = request.get_json()
    history = data.get("history", [])
    user_message = latest_user_message(history)
    if not user_message:
        logger.warning("chat_request_empty", extra={"remote_addr": get_remote_address()})
        return jsonify({"reply": "No question received."}), 400

    start_time = time.perf_counter()

    # Phase 5: one RunRecord per request. Collects every stage's outcome and
    # is written to STAGE_LOG_FILE no matter how this request ends (see
    # the `finally` block below). run.write() (called in `finally`) also
    # emits one structured log line per stage to stdout - that's what shows
    # up in the Render dashboard, since Render never sees STAGE_LOG_FILE.
    run = RunRecord(query=user_message, log_path=STAGE_LOG_FILE)
    logger.info(
        "chat_request_received",
        extra={"request_id": run.request_id, "query": user_message[:200]},
    )

    try:
        # ---- stage: route -------------------------------------------------
        r_route = run_stage(
            "route",
            lambda: route_question(user_message, client, MODEL_ID),
            policy=API_POLICY,   # network LLM call -> worth retrying
        )
        run.add(r_route, extra={})
        if r_route.status is StageStatus.FAILED:
            raise RuntimeError(f"route stage failed: {r_route.error}")
        route = r_route.payload

        if route == "sql" and SCHEMA_TEXT is not None:
            # ---- stage: sql ------------------------------------------------
            # answer_question_multi() internally swallows bad-SQL execution
            # errors on purpose (returns a friendly "sorry" message instead of
            # crashing) but its two LLM calls (write SQL, phrase the answer)
            # raise normally on network failure, so retries work correctly.
            r_sql = run_stage(
                "sql",
                lambda: answer_question_multi(
                    user_message, SCHEMA_TEXT, client, DB_PATHS, model=MODEL_ID
                ),
                policy=API_POLICY,
            )
            run.add(r_sql, extra={"sql": (r_sql.payload or {}).get("sql")})
            if r_sql.status is StageStatus.FAILED:
                raise RuntimeError(f"sql stage failed: {r_sql.error}")
            reply = r_sql.payload["answer"]

        else:
            route = "rag"

            # ---- stage: retrieve -------------------------------------------
            # DEFAULT_POLICY (no retries): since D1's revision the embedding
            # model runs in-process, so this stage is pure local compute.
            # There is no network call here left to flake.
            r_retr = run_stage(
                "retrieve",
                lambda: retrieve(user_message) if user_message else [],
                policy=DEFAULT_POLICY,
                is_empty=lambda res: len(res) == 0,
            )
            chunk_ids = [c["chunk_id"] for c in (r_retr.payload or [])]
            run.add(r_retr, extra={"chunk_ids": chunk_ids})
            if r_retr.status is StageStatus.FAILED:
                raise RuntimeError(f"retrieve stage failed: {r_retr.error}")
            snippets = r_retr.payload or []   # [] on EMPTY, same as before

            # ---- stage: generate ---------------------------------------
            r_gen = run_stage(
                "generate",
                lambda: generate_rag_answer(user_message, history, snippets),
                policy=API_POLICY,
            )
            run.add(r_gen, extra={})
            if r_gen.status is StageStatus.FAILED:
                raise RuntimeError(f"generate stage failed: {r_gen.error}")
            reply = r_gen.payload

        answer_time = time.perf_counter() - start_time
        log_exchange(user_message, route, answer_time)
        logger.info(
            "chat_request_completed",
            extra={
                "request_id": run.request_id,
                "route": route,
                "status": run.overall_status(),
                "total_duration_s": round(answer_time, 2),
            },
        )
        return jsonify({
            "reply": reply,
            "route": route,
            "answer_time": round(answer_time, 2),
        })

    except Exception as error:
        # Unchanged user-facing behavior: still a friendly 500.
        logger.error(
            "chat_request_failed",
            extra={
                "request_id": run.request_id,
                "failed_stage": run.failed_stage(),
            },
            exc_info=True,
        )
        return jsonify({"reply": f"Sorry, something went wrong: {error}"}), 500

    finally:
        # Always write the stage log - success or failure - so a crashed
        # request still leaves a full trail of which stage broke. This also
        # emits the same information as structured log lines to stdout.
        run.write()


@app.errorhandler(429)
def ratelimit_handler(error):
    logger.warning(
        "rate_limit_exceeded",
        extra={"remote_addr": get_remote_address(), "detail": str(error)},
    )
    return jsonify({"reply": "Too many requests. Please slow down and try again shortly."}), 429


def main():
    # Render (and Cloud Run) inject the port to listen on via $PORT.
    # Falls back to 7860 for local runs / HF Spaces (see chatbot.config).
    app.run(host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()

#Test