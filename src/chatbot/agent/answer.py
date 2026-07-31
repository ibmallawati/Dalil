"""
answer.py — D5 (structured card) and L3 (grounding self-check), in ONE call.

D5 asks the model to extract a checklist from the retrieved sections. L3
asks it to judge whether those sections actually ground the answer, and to
say confidence:"none" when they do not.

These are one request, not two. A second call would double the free-tier
quota burn and add a second thing that can rate-limit mid-demo, for a
judgement the model is already making implicitly while it extracts.

--- Defence in depth, because a prompt is not a guarantee ---------------

The prompt instructs the model not to invent. That instruction is
necessary and not sufficient — a model asked for six fields tends to
produce six plausible fields. So three independent mechanisms stack:

  1. WHOLE-SERVICE RETRIEVAL (chatbot.agent.router)
     Every section of the resolved service is loaded, so the context is
     not missing fields that the corpus actually has. Top-k retrieval was
     the thing that structurally forced the model to fill gaps.

  2. THE PROMPT (here)
     JSON-only, Arabic-only, use only the supplied text, mark anything
     absent as «غير محدد في المصدر», report confidence honestly.

  3. THE POST-PASS (chatbot.agent.card.apply_missing_field_policy)
     Any field whose '##' section is absent from the CONTEXT is
     overwritten regardless of what the model returned. source_url is
     taken from chunk metadata, never from the model. This one is
     structural: it holds even if the model ignores the prompt entirely.

Only the third is a guarantee. The first two make it rarely needed.

--- Never blank the screen ----------------------------------------------

Every parse path has a fallback. A malformed response degrades to prose
with the same source citation; a failed call degrades to an honest refusal.
Nothing here can return nothing.
"""

import json
import logging
import re

from chatbot.agent.card import (
    CARD_FIELDS,
    NOT_IN_SOURCE,
    apply_missing_field_policy,
    empty_card,
    metadata_from,
)
from chatbot.llm import chat, usage_of

logger = logging.getLogger(__name__)

# Models wrap JSON in ```json fences despite being told not to. Cheap to
# strip, and stripping it turns a "failed" answer into a good one.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# Last resort: find the outermost {...} in a reply with prose around it.
_BRACES = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = """أنت «دليل»، مساعد ذكي للخدمات الحكومية في سلطنة عُمان.

اللغة: العربية فقط. لا تستخدم الإنجليزية إطلاقاً.

أعد كائن JSON واحداً فقط، دون أي نص قبله أو بعده، ودون علامات ```.

المفاتيح المطلوبة:
- eligibility: نص — الفئة المستهدفة بالخدمة
- required_documents: مصفوفة نصوص — المستندات المطلوبة
- steps: مصفوفة نصوص — خطوات إنجاز المعاملة بالترتيب
- fees: نص — الرسوم
- estimated_time: نص — المدة المتوقعة
- where: نص — أين تتم المعاملة
- confidence: واحد من "high" أو "partial" أو "none"

قواعد صارمة:
1. استخدم المعلومات المرفقة أدناه فقط. لا تستخدم معرفتك العامة إطلاقاً.
2. لا تخترع أي رسوم أو مستندات أو مدد زمنية. هذه معلومات حكومية،
   والمعلومة الخاطئة تعني رحلة ضائعة على المواطن.
3. أي حقل لا تجد له إجابة في المعلومات المرفقة اكتب فيه بالضبط:
   «غير محدد في المصدر»
4. انقل الرسوم والمدد كما وردت حرفياً دون إعادة صياغة.
5. confidence:
   - "high"    إذا كانت المعلومات المرفقة تجيب على سؤال المستخدم
   - "partial" إذا كانت تجيب على جزء منه فقط
   - "none"    إذا كانت لا تحتوي على إجابة السؤال إطلاقاً
   إذا كانت الإجابة "none" فلا تخترع إجابة — اترك الحقول
   «غير محدد في المصدر»."""


def build_context(chunks):
    """The retrieved sections, as the model will see them."""
    if not chunks:
        return ""
    first = chunks[0]
    header = f"الجهة: {first.get('authority', '')} | الخدمة: {first.get('service', '')}"
    body = "\n".join(f"## {c.get('section', '')}\n{c.get('text', '')}" for c in chunks)
    return f"{header}\n\n{body}"


def parse_card(text):
    """
    Turn a model reply into a dict, or None.

    Three attempts before giving up, because each recovers a response that
    is correct but wrapped: raw JSON, fenced JSON, then JSON embedded in
    prose. Returning None here means the prose fallback runs — the screen
    is never blank either way, but a recovered card is a much better answer.
    """
    if not text:
        return None
    for candidate in (text.strip(),
                      (_FENCE.match(text).group(1) if _FENCE.match(text) else None),
                      (_BRACES.search(text).group(0) if _BRACES.search(text) else None)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def prose_fallback(text, chunks):
    """
    A malformed JSON reply still contains a usable Arabic answer. Show it
    as prose with the real citation rather than discarding it.

    D5 says a malformed response must never blank the screen mid-demo.
    """
    cleaned = (text or "").strip()
    match = _FENCE.match(cleaned)
    if match:
        cleaned = match.group(1).strip()
    return {
        "format": "prose",
        "text": cleaned or "تعذر إنتاج إجابة منسقة.",
        **metadata_from(chunks),
        "confidence": "partial" if chunks else "none",
    }


def generate_answer(question, chunks, history=None, model=None):
    """
    Produce one answer. Returns a dict the UI renders directly.

    `format` is 'card' or 'prose'. Both carry the same provenance fields,
    so the citation is present either way.
    """
    if not chunks:
        # Nothing retrieved: there is nothing to ground an answer in, and
        # asking the model anyway is asking it to improvise.
        return {"format": "card", **empty_card(confidence="none")}

    context = build_context(chunks)
    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\nالمعلومات:\n" + context}]
    # Prior turns give the model the thread of the conversation (e.g. the
    # citizen's answer to a clarifying question) without becoming grounding:
    # the system prompt above is explicit that only «المعلومات» may be used.
    messages.extend(history or [])
    messages.append({"role": "user", "content": question})

    # `model` pins generation to one model, bypassing the failover chain.
    # The D7 harness uses it to keep the demo model's daily quota intact.
    completion = chat(messages, model=model)
    usage = usage_of(completion)
    raw = completion.choices[0].message.content or ""

    logger.info("answer_generated", extra={
        "served_by": usage.get("served_by"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "finish_reason": usage.get("finish_reason"),
        "sections": len(chunks),
    })

    # A truncated reply cannot be valid JSON. Say so explicitly rather than
    # letting it surface as a mysterious parse failure.
    if usage.get("finish_reason") == "length":
        logger.warning("answer_truncated", extra={
            "hint": "raise LLM_MAX_TOKENS; reasoning tokens count against it",
            "completion_tokens": usage.get("completion_tokens"),
        })

    parsed = parse_card(raw)
    if parsed is None:
        logger.warning("answer_json_parse_failed",
                       extra={"raw_head": raw[:200], "served_by": usage.get("served_by")})
        return {**prose_fallback(raw, chunks), "_served_by": usage.get("served_by")}

    card = apply_missing_field_policy(parsed, chunks)
    overwritten = [f.key for f in CARD_FIELDS
                   if card[f.key] == NOT_IN_SOURCE and parsed.get(f.key)
                   not in (None, "", [], NOT_IN_SOURCE)]
    if overwritten:
        # The model supplied a value for a field the context does not
        # support. The post-pass caught it. Worth an explicit line: this is
        # a hallucination that was blocked, and it is evidence the layer
        # earns its place.
        logger.warning("ungrounded_fields_overwritten",
                       extra={"fields": overwritten, "served_by": usage.get("served_by")})

    return {"format": "card", **card, "_served_by": usage.get("served_by")}
