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
# Covers three client libraries this project actually uses:
#   - requests/httpx (huggingface_hub's embed call)  -> Timeout, ConnectionError, ...
#   - the openai library (route + generate + SQL-gen) -> APITimeoutError, APIConnectionError
# These carry NO HTTP status code, so they MUST be matched by name.
RETRYABLE_EXC_NAMES = {
    "Timeout", "ConnectTimeout", "ReadTimeout",
    "ConnectionError", "ConnectError", "RemoteProtocolError",
    "APITimeoutError", "APIConnectionError",   # <- openai library
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


def classify(exc) -> Verdict:
    status = _extract_status(exc)

    if status in RETRYABLE_STATUS:
        return Verdict.RETRY
    if status in TERMINAL_STATUS:
        return Verdict.TERMINAL
    if type(exc).__name__ in RETRYABLE_EXC_NAMES:
        return Verdict.RETRY

    # Unknown error (e.g. a real bug in our own code): fail fast.
    # Retrying the same input through the same bug just burns quota.
    return Verdict.TERMINAL