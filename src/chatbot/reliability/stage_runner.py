"""
stage_runner.py — the 'worker' of the fault-tolerance layer.

Given a stage name, a function to run, and a retry policy, it:
  - runs the function,
  - on error, asks the classifier whether to retry,
  - applies backoff between retries,
  - detects a legitimate EMPTY result (not an error),
  - returns a StageResult describing exactly what happened.

It leans on failure_classifier for the "is this worth retrying?" decision.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from chatbot.reliability.failure_classifier import classify, Verdict


class StageStatus(Enum):
    OK = "ok"                  # worked on the first try
    RETRIED_OK = "retried_ok"  # failed then succeeded on a later try
    FAILED = "failed"          # gave up (terminal error, or retries exhausted)
    EMPTY = "empty"            # ran fine but returned nothing useful (valid!)


@dataclass(frozen=True)
class RetryPolicy:
    """
    delays = the sleep (seconds) BETWEEN attempts.
      - len(delays) == number of retries
      - total attempts == len(delays) + 1
    So delays=() means 'one attempt, fail fast'.
    """
    delays: tuple = ()


# The house rule: most stages are local/deterministic -> don't retry.
DEFAULT_POLICY = RetryPolicy(delays=())

# The one stage that actually flakes: the HF network call. Your 1/2/4/8s.
HF_POLICY = RetryPolicy(delays=(1, 2, 4, 8))


@dataclass
class StageResult:
    name: str
    status: StageStatus
    attempts: int
    duration_s: float
    payload: Any = None            # the return value on success
    error: Optional[str] = None    # short error description on failure
    delays_used: list = field(default_factory=list)  # for logging/inspection


# `sleeper` is injectable so tests don't actually wait 15 seconds.
def run_stage(
    name: str,
    fn: Callable[[], Any],
    policy: RetryPolicy = DEFAULT_POLICY,
    is_empty: Optional[Callable[[Any], bool]] = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> StageResult:

    start = time.perf_counter()
    delays_used = []
    attempt = 0
    last_error = None

    while True:
        attempt += 1
        try:
            result = fn()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            verdict = classify(exc)

            # Terminal, or no retries left -> stop and report FAILED.
            if verdict is Verdict.TERMINAL or len(delays_used) >= len(policy.delays):
                return StageResult(
                    name=name, status=StageStatus.FAILED, attempts=attempt,
                    duration_s=round(time.perf_counter() - start, 3),
                    error=last_error, delays_used=delays_used,
                )

            # Retryable and retries remain -> back off, then loop.
            delay = policy.delays[len(delays_used)]
            delays_used.append(delay)
            sleeper(delay)
            continue

        # --- success path ---
        # A valid 'nothing found' is EMPTY, not a crash, and never retried.
        if is_empty is not None and is_empty(result):
            status = StageStatus.EMPTY
        else:
            status = StageStatus.RETRIED_OK if attempt > 1 else StageStatus.OK

        return StageResult(
            name=name, status=status, attempts=attempt,
            duration_s=round(time.perf_counter() - start, 3),
            payload=result, delays_used=delays_used,
        )
