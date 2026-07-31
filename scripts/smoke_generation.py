"""
Step 2 isolation smoke test: does the configured generation endpoint work?

Tests ONLY generation — no retrieval, no Flask, no index. If this passes,
the provider swap works; if the app still misbehaves, the fault is
elsewhere. That separation is the point of running it standalone.

    python scripts/smoke_generation.py

Reads LLM_* from .env, so it verifies the exact configuration the app will
use rather than a hardcoded one. Point it at Ollama by editing .env and
re-running — no code change.

Three checks, because "it returned 200" is not the same as "it works":

  1. it answers at all, through the real retry policy
  2. it answers in ARABIC (a model silently replying in English breaks
     Daleel's Arabic-only requirement without erroring)
  3. finish_reason is 'stop', not 'length' — and reasoning tokens are
     budgeted. On a thinking model an unbudgeted max_tokens truncates the
     reply, which for D5's JSON card means it never parses.
"""

import io
import sys
import time

# Windows consoles default to cp1252, which cannot encode Arabic. Without
# this the script crashes on printing a successful reply — failing at the
# last step of a call that actually worked.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from chatbot.config import settings
from chatbot.llm import chat, usage_of
from chatbot.logging_config import configure_logging
from chatbot.reliability.stage_runner import API_POLICY, StageStatus, run_stage

PROMPT = "أجب بجملة واحدة قصيرة بالعربية فقط: ما هي عاصمة سلطنة عُمان؟"


def main():
    configure_logging()

    print(f"base_url  : {settings.llm_base_url}")
    print(f"model     : {settings.llm_model}")
    print(f"effort    : {settings.llm_reasoning_effort or '(not sent)'}")
    print(f"max_tokens: {settings.llm_max_tokens}")
    print(f"key       : {'set (' + str(len(settings.llm_api_key)) + ' chars)' if settings.llm_api_key else 'MISSING'}")
    print("-" * 66)

    start = time.perf_counter()
    # Through the real retry policy, so a transient 503 is handled the same
    # way it would be during a demo rather than failing the smoke test.
    result = run_stage(
        "generate",
        lambda: chat([{"role": "user", "content": PROMPT}]),
        policy=API_POLICY,
    )
    elapsed = time.perf_counter() - start

    if result.status is StageStatus.FAILED:
        print(f"FAILED    : {result.error}")
        print(f"attempts  : {result.attempts}  delays: {result.delays_used}")
        print(f"elapsed   : {elapsed:.1f}s")
        print("\nA 503 here means the model is overloaded, not that the setup is")
        print("wrong — try another LLM_MODEL (gemini-3.6-flash,")
        print("gemini-3.5-flash-lite). A 401 means the key; a 404 means the")
        print("model id; a 429 means the free-tier quota.")
        return 1

    completion = result.payload
    reply = (completion.choices[0].message.content or "").strip()
    usage = usage_of(completion)

    print(f"reply     : {reply}")
    print(f"latency   : {elapsed:.2f}s   attempts: {result.attempts}")
    print(f"tokens    : {usage}")

    checks = []

    arabic = sum(1 for c in reply if "؀" <= c <= "ۿ")
    checks.append((arabic > 5, f"replied in Arabic ({arabic} Arabic chars)"))

    checks.append((usage.get("finish_reason") == "stop",
                   f"finish_reason is 'stop' (got {usage.get('finish_reason')!r})"))

    reasoning = usage.get("reasoning_tokens", 0)
    checks.append((reasoning == 0,
                   f"reasoning tokens budgeted (spent {reasoning}; "
                   f"set LLM_REASONING_EFFORT=minimal if this is high)"))

    print("-" * 66)
    for ok, label in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")

    passed = all(ok for ok, _ in checks)
    print(f"\n{'SMOKE TEST PASSED' if passed else 'SMOKE TEST FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
