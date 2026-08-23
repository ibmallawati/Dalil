"""
answer.py — generation (D5) and the grounding self-check (L3), in ONE call.

The model is given the retrieved passages and asked for two things: an
answer to the question actually asked, and an honest verdict on whether the
passages supported it.

One request, not two. A second call would double free-tier quota burn and
add another thing that can rate-limit mid-demo, for a judgement the model is
already making implicitly while it writes.

--- The answer shape follows the question --------------------------------

This used to emit a fixed six-field card and pad every gap with «غير محدد
في المصدر». It now returns free-form Arabic, because:

  - «كم الرسوم؟» deserves the fee, not six sections;
  - «ما الفرق بين التجديد والإصدار؟» is a comparison, which no per-service
    template can express;
  - a question spanning two authorities needs an answer spanning two, and
    the retrieval layer now supplies exactly that.

The model chooses the form — a sentence, a list, numbered steps — from a
narrow markdown subset the UI renders as real DOM nodes.

--- Answering the next question too -------------------------------------

A literal answer is often a useless one. «أين أجدد بطاقتي؟» answered with
only a location sends someone to a counter without the right paper, and
the fee and document list were sitting in the same retrieved passage.

So the prompt asks for the direct answer FIRST, then whatever genuinely
helps finish the task — bounded by two rules that keep this from becoming
the card again:

  1. only from the retrieved passages, never from general knowledge;
  2. only what relates to THIS question, never everything available.

The distinction from the old card is what drives the selection. The card
emitted the same six fields whatever was asked; this picks its additions
from the question, so «كم الرسوم؟» and «كيف أسجل؟» produce different
shapes as well as different content. Identical structure across answers is
the signal that it has regressed into a template.

--- What did NOT change, and must not ------------------------------------

Dropping the template is not the same as dropping grounding. Three things
still hold:

  1. ONLY the supplied passages may be used. No general knowledge.
  2. Silence is an answer. If the passages do not cover it, say so — never
     fill the gap from training data. A wrong government fee costs a citizen
     a wasted trip to a counter.
  3. PROVENANCE IS NOT THE MODEL'S. Sources are attached from chunk
     metadata after generation, overwriting anything the model wrote. A
     fabricated citation survives a glance in a way a fabricated fact does
     not, which makes it the more dangerous of the two.

--- Citing what was used, not what was retrieved --------------------------

Retrieval got wider when L1 stopped bypassing it, and that broke something
subtle. «كم رسوم تجديد جواز السفر ورخصة السياقة؟» retrieves five services;
the answer uses two. Citing all five puts «تجديد رخصة مرشد سياحي» under an
answer about passports, and a citation next to a fee is a claim that the
fee came from there.

So the model reports which passages it drew on, and only those are cited.
This does NOT hand it provenance: it selects from the passages it was
given and cannot introduce a source that is not in the corpus. Every URL
and title still comes from chunk metadata. An unusable or empty selection
falls back to citing everything — over-citing is a flaw, under-citing is a
missing attribution.

--- Never blank the screen -----------------------------------------------

Every parse path has a fallback: malformed JSON degrades to the raw text
with its citations intact, a failed call degrades to an honest refusal.
Nothing here can return nothing.
"""

import json
import logging
import re

from chatbot.agent.card import metadata_from, normalise_confidence
from chatbot.llm import chat, usage_of

logger = logging.getLogger(__name__)

# Models wrap JSON in ```json fences despite being told not to. Cheap to
# strip, and stripping it turns a "failed" answer into a good one.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# Last resort: the outermost {...} in a reply with prose around it.
_BRACES = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = """أنت «دليل»، مساعد يجيب عن إجراءات الخدمات الحكومية في سلطنة عُمان.

اللغة: العربية فقط.

أعد كائن JSON واحداً فقط، دون أي نص قبله أو بعده، ودون علامات ```:

{
  "answer": "الإجابة",
  "confidence": "high" أو "partial" أو "none",
  "used": [أرقام النصوص التي استندت إليها فعلاً]
}

قواعد الإجابة:
1. ابدأ بالإجابة المباشرة. أول سطر يجيب عن السؤال ولا يمهّد له.

2. ثم أضف ما يحتاجه المواطن فعلاً لإنجاز معاملته — بشرط أن يكون في
   النصوص المرفقة. اسأل نفسك: «ما الذي سيسأل عنه بعد هذا مباشرة؟»
   من سأل «أين أجدد بطاقتي؟» ينفعه أن يعرف الرسوم والمستندات قبل أن
   يذهب، لا بعد أن يصل ويُرَدّ.

   شرطان لا يُخالَفان:
   - لا تضف معلومة غير موجودة في النصوص. السكوت أفضل من التخمين.
   - لا تضف معلومة لا صلة لها بالسؤال لمجرد أنها متاحة أمامك.
   وإن لم تجد إضافة تنفعه، اكتفِ بالإجابة المباشرة وتوقّف.

3. لا يوجد قالب، والإضافة تختلف باختلاف السؤال:
   - «كم الرسوم؟» ← المبلغ، ثم ما يغيّره (الفئة، المدة) إن ورد.
   - «أين أجدد؟» ← المكان، ثم ما يجب إحضاره معه.
   - «كيف أسجّل؟» ← الخطوات، ثم المدة المتوقعة.
   إذا تشابهت إجاباتك في الشكل والترتيب، فأنت تملأ قالباً لا تجيب سؤالاً.

4. اختر البنية التي يفرضها المحتوى نفسه: جملة واحدة لسؤال بسيط، خطوات
   مرقّمة لإجراء، قائمة قصيرة لعناصر متوازية، سطر **بارز** يفصل مجموعتين.
   الإجابة من سطر واحد إجابة صحيحة إذا كان السؤال يستحق سطراً واحداً.

5. إذا كان السؤال يخص أكثر من خدمة أو جهة، اذكرها جميعاً بوضوح.

قواعد المصدر — صارمة:
6. استخدم النصوص المرفقة فقط. لا تضف أي معلومة من معرفتك العامة إطلاقاً.
   هذه القاعدة تسري على الإضافات في البند 2 كما تسري على الإجابة نفسها.
7. لا تخترع رسوماً أو مستندات أو مدداً زمنية. هذه معلومات حكومية،
   والمعلومة الخاطئة تعني رحلة ضائعة على المواطن.
8. إذا كانت النصوص لا تغطي جزءاً من السؤال، قل ذلك صراحةً بدل تخمينه.
9. انقل الأرقام والرسوم كما وردت حرفياً. لا تضف وحدة عملة لم ترد في النص.
10. لا تذكر روابط أو أسماء مصادر داخل الإجابة — تُضاف تلقائياً بعد ذلك.

confidence:
  "high"    النصوص تجيب عن السؤال بالكامل
  "partial" تجيب عن جزء منه فقط
  "none"    لا تحتوي على إجابة السؤال

used — حقل إلزامي، لا تُغفله أبداً:
  كل نص مرفق مسبوق برقم بين قوسين مثل [3].
  اذكر أرقام النصوص التي استعملتها في الإجابة فقط.
  لا تذكر رقم نص قرأته ولم تستفد منه — الأرقام تُستخدم لعرض المصادر
  أسفل الإجابة، وذكر مصدر لم يُستعمل يوحي بأنه سند لمعلومة ليست منه.
  حتى لو استعملت نصاً واحداً، اكتب "used": [رقمه]. إغفال هذا الحقل
  يجعل الإجابة تُنسب إلى مصادر لا علاقة لها بها.

تنسيق مسموح داخل answer: سطر يبدأ بـ - لعنصر قائمة، أو 1. لخطوة مرقّمة،
و**نص** للتأكيد. لا تستخدم أي تنسيق آخر."""

# Said when there is nothing retrieved at all. Not a model call — there is
# nothing to ground an answer in, and asking anyway invites improvisation.
NO_CONTEXT_REPLY = (
    "لا تتوفر لدي معلومات موثّقة تجيب عن هذا السؤال ضمن المصادر المتاحة."
)


def build_context(chunks):
    """
    The retrieved passages, numbered and labelled.

    The label lets the model attribute correctly when an answer spans more
    than one service; the number lets it say which passages it actually
    used, so the citations under the answer match the answer.
    """
    if not chunks:
        return ""
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        header = " | ".join(
            part for part in (chunk.get("authority"), chunk.get("service"),
                              chunk.get("section")) if part
        )
        blocks.append(f"### [{index}] {header}\n{chunk.get('text', '')}")
    return "\n\n".join(blocks)


_SHINGLE = 5


def _words(text):
    return re.sub(r"[^\w\s]", " ", text or "", flags=re.UNICODE).split()


def traced_chunks(chunks, answer):
    """
    Which passages the answer demonstrably came from, by text overlap.

    The fallback for a model that omits `used`. It does omit it — observed
    on 2 of 4 turns in one conversation, always on the short answers — and
    the previous fallback of "cite everything" then put eight services under
    a one-line fee, including «التقارير والمؤشرات الاقتصادية» beneath an
    answer about passport documents.

    Overlap works here because the prompt already requires fees, documents
    and durations to be copied verbatim, so a used passage and the answer
    share long runs of words. A five-word run is far past coincidence in
    Arabic prose; shorter passages are matched whole.
    """
    haystack = " ".join(_words(answer))
    if not haystack:
        return []
    traced = []
    for chunk in chunks:
        words = _words(chunk.get("text", ""))
        if not words:
            continue
        size = min(_SHINGLE, len(words))
        shingles = (" ".join(words[i:i + size])
                    for i in range(len(words) - size + 1))
        if any(shingle in haystack for shingle in shingles):
            traced.append(chunk)
    return traced


def _reported(chunks, used):
    """The model's own `used` list, resolved to chunks."""
    if not isinstance(used, (list, tuple)):
        return []
    picked = []
    for value in used:
        try:
            index = int(value) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(chunks) and chunks[index] not in picked:
            picked.append(chunks[index])
    return picked


def used_chunks(chunks, used, answer=""):
    """
    Narrow `chunks` to the passages the answer actually rests on.

    Two independent signals, because neither alone is trustworthy. Measured
    across one four-turn conversation, the model's self-report was wrong in
    BOTH directions on the same prompt:

      - omitted entirely on two turns;
      - on a re-run of the same turns, listing nearly every passage it had
        read, which put «التقارير والمؤشرات الاقتصادية» under an answer
        about passport documents.

    Text tracing is objective evidence and the prompt already requires fees,
    documents and durations to be copied verbatim — precisely the facts a
    citation has to back. So where both signals exist, their INTERSECTION
    is cited: the model claims it, and the text proves it.

    The order of the fallbacks is a claim about which evidence is stronger,
    and tracing outranks the self-report. What is never returned is every
    retrieved chunk: a citation beside a government fee says that fee came
    from there, and eight ministries under one fee is a false claim, not a
    generous one.
    """
    reported = _reported(chunks, used)
    traced = traced_chunks(chunks, answer)

    both = [c for c in traced if c in reported]
    if both:
        return both
    if traced:
        logger.info("answer_citations_traced", extra={
            "hint": "cited by text overlap; model's 'used' disagreed or was absent",
            "traced": len(traced), "reported": len(reported),
            "retrieved": len(chunks)})
        return traced
    if reported:
        return reported

    # Nothing reported and nothing traceable. The best-matching passage is
    # the honest single guess; listing the rest would invent agreement
    # between sources that were never consulted.
    logger.warning("answer_citations_unresolved",
                   extra={"hint": "no 'used', no text overlap; citing top hit",
                          "retrieved": len(chunks)})
    return chunks[:1]


def parse_answer(text):
    """
    Turn a model reply into a dict, or None.

    Three attempts, because each recovers a response that is correct but
    wrapped: raw JSON, fenced JSON, JSON embedded in prose.
    """
    if not text:
        return None
    candidates = [text.strip()]
    fenced = _FENCE.match(text)
    if fenced:
        candidates.append(fenced.group(1))
    braced = _BRACES.search(text)
    if braced:
        candidates.append(braced.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("answer"):
            return parsed
    return None


def _fallback(raw, chunks):
    """
    Unparseable JSON still usually contains a usable Arabic answer. Show it
    rather than discarding it — with the same citations attached.
    """
    cleaned = (raw or "").strip()
    fenced = _FENCE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    # Strip a bare JSON skeleton if the model emitted keys but broke syntax.
    cleaned = re.sub(r'^\s*[\{\}"]+\s*|\s*[\{\}"]+\s*$', "", cleaned).strip()
    return {
        "format": "answer",
        "answer": cleaned or "تعذر إنتاج إجابة منسقة.",
        "confidence": "partial" if chunks else "none",
        **metadata_from(chunks),
    }


def generate_answer(question, chunks, history=None, model=None):
    """
    Produce one answer. Returns a dict the UI renders directly.

    Always has `answer` (Arabic text), `confidence`, and a `sources` list.
    """
    if not chunks:
        return {
            "format": "answer",
            "answer": NO_CONTEXT_REPLY,
            "confidence": "none",
            **metadata_from([]),
        }

    messages = [{
        "role": "system",
        "content": SYSTEM_PROMPT + "\n\nالنصوص المتاحة:\n" + build_context(chunks),
    }]
    # Prior turns give the model the thread of the conversation (e.g. the
    # citizen's answer to a clarifying question) without becoming grounding:
    # rule 4 above is explicit that only the supplied passages may be used.
    messages.extend(history or [])
    messages.append({"role": "user", "content": question})

    # `model` pins generation to one model, bypassing the failover chain —
    # the eval harness uses it to keep the demo model's daily quota intact.
    completion = chat(messages, model=model)
    usage = usage_of(completion)
    raw = completion.choices[0].message.content or ""

    logger.info("answer_generated", extra={
        "served_by": usage.get("served_by"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "finish_reason": usage.get("finish_reason"),
        "chunks": len(chunks),
        "services": sorted({c.get("service_id") for c in chunks if c.get("service_id")}),
    })

    # A truncated reply cannot be valid JSON. Name it, so it does not
    # surface later as a mysterious parse failure.
    if usage.get("finish_reason") == "length":
        logger.warning("answer_truncated", extra={
            "hint": "raise LLM_MAX_TOKENS; reasoning tokens count against it",
            "completion_tokens": usage.get("completion_tokens"),
        })

    parsed = parse_answer(raw)
    if parsed is None:
        logger.warning("answer_json_parse_failed",
                       extra={"raw_head": raw[:200],
                              "served_by": usage.get("served_by")})
        return {**_fallback(raw, chunks), "_served_by": usage.get("served_by")}

    answer_text = str(parsed.get("answer", "")).strip()
    cited = used_chunks(chunks, parsed.get("used"), answer_text)
    if len(cited) < len(chunks):
        logger.info("answer_narrowed_citations", extra={
            "retrieved": len(chunks), "cited": len(cited),
            "services": sorted({c.get("service_id") for c in cited
                                if c.get("service_id")}),
        })

    return {
        "format": "answer",
        "answer": answer_text or NO_CONTEXT_REPLY,
        "confidence": normalise_confidence(parsed.get("confidence")),
        # Attached AFTER the model, overwriting anything it invented. The
        # model chose WHICH of these passages to cite; it never supplied the
        # URL, title or authority for any of them.
        **metadata_from(cited),
        "_served_by": usage.get("served_by"),
    }
