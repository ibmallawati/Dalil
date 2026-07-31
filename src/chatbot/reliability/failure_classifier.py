"""
failure_classifier.py — the 'brain' of the fault-tolerance layer.

Its ONLY job: look at an error and decide whether retrying it could help.
It knows nothing about stages, logging, or backoff. One decision, one place.
"""

from enum import Enum


class Verdict(Enum):
    RETRY = "retry"        # transient — trying again might work
    TERMINAL = "terminal"  # hopeless — trying again wastes time/quota


# --- Tuning knobs: which HTTP statuses mean what -------------------------

# Server hiccups + rate limits: worth another shot.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Client-side / auth / bad-request: retrying changes nothing.
TERMINAL_STATUS = {400, 401, 403, 404, 422}

# Network-level exceptions (matched by class name so we need no imports).
# Covers the client libraries this project actually uses:
#   - httpx, under the openai SDK          -> Timeout, ConnectError, ...
#   - the openai library itself            -> APITimeoutError, APIConnectionError
# These carry NO HTTP status code, so they MUST be matched by name.
#
# RateLimitError is listed defensively. It normally arrives WITH status 429
# and is already caught by RETRYABLE_STATUS above — but Daleel's generation
# now runs on a free tier where 429 is an expected, routine condition
# rather than an exceptional one, and a rate limit must never be
# misclassified as TERMINAL. Matching it by name as well means it stays
# retryable even if a provider omits the status code on the exception.
RETRYABLE_EXC_NAMES = {
    "Timeout", "ConnectTimeout", "ReadTimeout",
    "ConnectionError", "ConnectError", "RemoteProtocolError",
    "APITimeoutError", "APIConnectionError",   # <- openai library
    "RateLimitError",                          # <- free-tier quota (429)
}


def _extract_status(exc):
    """Pull an HTTP status code out of whatever exception shape we got."""
    status = getattr(exc, "status_code", None)   # some libs: exc.status_code
    if status is not None:
        return status
    resp = getattr(exc, "response", None)         # others: exc.response.status_code
    if resp is not None:
        return getattr(resp, "status_code", None)
    return None


# Free-tier quotas come in two shapes, and only one of them is worth
# waiting out:
#
#   per-minute  -> transient. Backing off is exactly right.
#   per-DAY     -> exhausted until midnight. No amount of backoff helps.
#
# Measured against Gemini's free tier: gemini-3.5-flash allows 20 requests
# per DAY. Hitting it sent the stage runner through all four retries for 77
# seconds before failing anyway. On stage that is 77 seconds of silence
# buying a failure that was certain from the first response.
#
# Google names the quota in the error body, so the distinction is readable
# rather than guessed.
DAILY_QUOTA_MARKERS = ("PerDay", "RequestsPerDay", "per day")


def _is_daily_quota(exc) -> bool:
    text = str(exc)
    return any(marker in text for marker in DAILY_QUOTA_MARKERS)


def classify(exc) -> Verdict:
    status = _extract_status(exc)

    if status == 429 and _is_daily_quota(exc):
        # Fail fast so the caller can surface a real message (or the demo
        # can switch to its fallback) instead of stalling.
        return Verdict.TERMINAL

    if status in RETRYABLE_STATUS:
        return Verdict.RETRY
    if status in TERMINAL_STATUS:
        return Verdict.TERMINAL
    if type(exc).__name__ in RETRYABLE_EXC_NAMES:
        return Verdict.RETRY

    # Unknown error (e.g. a real bug in our own code): fail fast.
    # Retrying the same input through the same bug just burns quota.
    return Verdict.TERMINAL