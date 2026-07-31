"""
llm.py — one place that builds the generation call.

Every LLM call in Daleel goes through chat(), so provider-specific
parameters are decided once instead of being copy-pasted into the routing,
clarifying and answering paths where they would inevitably drift.

--- Why reasoning_effort is not optional here ----------------------------

Gemini 3.x Flash is a thinking model and its reasoning tokens count against
max_tokens. Measured on gemini-3.6-flash: 305 reasoning tokens spent
answering "what is the capital of Oman", for a 12-token reply. Sizing
max_tokens for the answer therefore truncates the answer — and at
max_tokens=100 the model returned 'Information Retrieval', an internal
thought fragment served as the reply.

D5's JSON card is this project's long output. Truncated JSON does not
parse, so unbudgeted reasoning would push every answer onto the plain-prose
fallback while looking like a prompt problem.

reasoning_effort="minimal" measured at 0 reasoning tokens with an identical
answer, so it is the default.

--- Provider neutrality --------------------------------------------------

The parameter is only sent when configured non-empty, and a provider that
rejects it is handled once, here: on a 400 naming the parameter, the call
is retried without it and the process stops sending it. A local Ollama
server therefore needs no configuration change to work.
"""

import logging

from openai import BadRequestError, OpenAI

from chatbot.config import settings

logger = logging.getLogger(__name__)

client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)

# Flipped to False the first time a provider rejects reasoning_effort, so
# the fallback costs one wasted request per process rather than one per call.
_send_reasoning_effort = bool(settings.llm_reasoning_effort)


def chat(messages, *, model=None, max_tokens=None, temperature=0, **kwargs):
    """
    One chat-completion call. Returns the raw completion object.

    Raises normally on failure — retry and backoff belong to the Phase 5
    stage runner, which is the only thing that should decide what is worth
    trying again.
    """
    global _send_reasoning_effort

    params = {
        "model": model or settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "temperature": temperature,
        **kwargs,
    }
    if _send_reasoning_effort:
        params["reasoning_effort"] = settings.llm_reasoning_effort

    try:
        return client.chat.completions.create(**params)
    except BadRequestError as exc:
        if not _send_reasoning_effort or "reasoning_effort" not in str(exc):
            raise
        logger.warning(
            "reasoning_effort_unsupported",
            extra={"model": params["model"], "effort": settings.llm_reasoning_effort,
                   "hint": "provider rejected the parameter; continuing without it"},
        )
        _send_reasoning_effort = False
        params.pop("reasoning_effort", None)
        return client.chat.completions.create(**params)


def usage_of(completion):
    """
    Token accounting, including reasoning tokens the SDK does not surface
    directly. Logged per call because reasoning burn is invisible otherwise
    and it is what silently eats a free-tier quota.
    """
    usage = getattr(completion, "usage", None)
    if usage is None:
        return {}
    reasoning = usage.total_tokens - usage.prompt_tokens - usage.completion_tokens
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "reasoning_tokens": max(reasoning, 0),
        "finish_reason": completion.choices[0].finish_reason,
    }
