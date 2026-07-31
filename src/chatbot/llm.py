"""
llm.py — one place that builds the generation call.

Every LLM call in Daleel goes through chat(), so provider-specific
behaviour is decided once instead of being copy-pasted into the routing,
clarifying and answering paths where it would inevitably drift.

Two things are handled here, both discovered by measurement rather than
assumed:

--- 1. Reasoning tokens are charged against max_tokens -------------------

Gemini 3.x Flash are thinking models. Measured on gemini-3.6-flash: 305
reasoning tokens spent answering "what is the capital of Oman", for a
12-token reply. Sizing max_tokens for the ANSWER therefore truncates the
answer — and at max_tokens=100 the model returned 'Information Retrieval',
an internal thought fragment served as the reply.

D5's JSON card is this project's long output, and truncated JSON does not
parse, so unbudgeted reasoning would push every answer onto the plain-prose
fallback while looking like a prompt problem.

reasoning_effort="minimal" measured 0 reasoning tokens with an identical
answer. It is sent only when configured non-empty, and a provider that
rejects it is handled once, here — so a local Ollama server needs no
configuration change.

--- 2. Per-day quota exhaustion fails over to the next model -------------

Free-tier quotas are PER MODEL and some are per-DAY: gemini-3.5-flash
allows 20 requests a day. A quota that resets at midnight Pacific cannot be
waited out by any backoff, so the failure classifier calls it TERMINAL and
this module moves to the next model in the chain instead.

That chain is LLM_MODEL, comma-separated and ordered. Recovering from an
exhausted quota by editing .env is not something anyone should be doing in
front of a panel.

Exhausted models are remembered for the life of the process, so the cost of
discovering exhaustion is one wasted request per model per run rather than
one per call.
"""

import logging

from openai import BadRequestError, NotFoundError, OpenAI

from chatbot.config import settings
from chatbot.reliability.failure_classifier import Verdict, classify

logger = logging.getLogger(__name__)

client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)

# Flipped off the first time a provider rejects reasoning_effort.
_send_reasoning_effort = bool(settings.llm_reasoning_effort)

# Models known to be unusable for the rest of this process: daily quota
# spent, or the id was rejected outright.
_unavailable = {}


def available_models():
    """The failover chain, minus anything already known to be unusable."""
    return [m for m in settings.llm_models if m not in _unavailable]


def _call(model, params):
    params = dict(params, model=model)
    if _send_reasoning_effort:
        params["reasoning_effort"] = settings.llm_reasoning_effort
    return client.chat.completions.create(**params)


def chat(messages, *, model=None, max_tokens=None, temperature=0, **kwargs):
    """
    One chat-completion call, with automatic model failover.

    Raises only when EVERY model in the chain has failed. Transient errors
    (503, per-minute 429, connection blips) propagate unchanged so the
    Phase 5 stage runner can decide whether to retry — that decision belongs
    to one place, and it is not this one.
    """
    global _send_reasoning_effort

    base = {
        "messages": messages,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "temperature": temperature,
        **kwargs,
    }

    candidates = [model] if model else available_models()
    if not candidates:
        raise RuntimeError(
            "Every model in LLM_MODEL is unavailable: "
            + "; ".join(f"{m} ({why})" for m, why in _unavailable.items())
        )

    last_exc = None
    for index, candidate in enumerate(candidates):
        try:
            completion = _call(candidate, base)
        except BadRequestError as exc:
            # A provider that rejects reasoning_effort: drop it and retry
            # the same model once, then stop sending it entirely.
            if _send_reasoning_effort and "reasoning_effort" in str(exc):
                logger.warning(
                    "reasoning_effort_unsupported",
                    extra={"model": candidate,
                           "effort": settings.llm_reasoning_effort,
                           "hint": "provider rejected it; continuing without"},
                )
                _send_reasoning_effort = False
                completion = _call(candidate, base)
            else:
                raise
        except NotFoundError as exc:
            # A wrong or retired model id. Never recovers; try the next.
            _unavailable[candidate] = "unknown model id"
            logger.error("llm_model_not_found",
                         extra={"model": candidate, "error": str(exc)[:200]})
            last_exc = exc
            continue
        except Exception as exc:
            # Only a TERMINAL verdict means "this model is done". A per-day
            # 429 lands here; a per-minute 429 or a 503 does not, and is
            # re-raised for the stage runner to back off on.
            if classify(exc) is Verdict.TERMINAL and _is_quota(exc):
                _unavailable[candidate] = "daily quota exhausted"
                remaining = candidates[index + 1:]
                logger.warning(
                    "llm_model_quota_exhausted",
                    extra={"model": candidate, "failing_over_to": remaining or None,
                           "error": str(exc)[:200]},
                )
                last_exc = exc
                continue
            raise

        if index > 0:
            logger.warning("llm_failover_served",
                           extra={"served_by": candidate,
                                  "skipped": candidates[:index]})
        return completion

    raise last_exc


def _is_quota(exc):
    status = getattr(exc, "status_code", None)
    return status == 429 or "429" in str(exc)


def usage_of(completion):
    """
    Token accounting, including reasoning tokens the SDK does not surface
    directly, and WHICH model actually answered.

    Reasoning burn is invisible otherwise, and it is what silently drains a
    free-tier quota. served_by matters because failover means the model that
    answered is not necessarily the one that was configured.
    """
    served_by = getattr(completion, "model", None)
    usage = getattr(completion, "usage", None)
    if usage is None:
        return {"served_by": served_by}
    reasoning = usage.total_tokens - usage.prompt_tokens - usage.completion_tokens
    return {
        "served_by": served_by,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "reasoning_tokens": max(reasoning, 0),
        "finish_reason": completion.choices[0].finish_reason,
    }
