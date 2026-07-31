"""
Tests for chatbot.reliability.failure_classifier - the retry/terminal verdict.
"""

import pytest

from src.chatbot.reliability.failure_classifier import Verdict, classify


class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _ResponseError(Exception):
    """Mimics libraries that carry the status on exc.response.status_code."""

    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.response = type("Resp", (), {"status_code": status_code})()


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_status_codes(status):
    assert classify(_StatusError(status)) is Verdict.RETRY


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_terminal_status_codes(status):
    assert classify(_StatusError(status)) is Verdict.TERMINAL


def test_status_on_response_attribute_is_also_read():
    assert classify(_ResponseError(503)) is Verdict.RETRY
    assert classify(_ResponseError(404)) is Verdict.TERMINAL


@pytest.mark.parametrize(
    "exc_name",
    ["Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError",
     "ConnectError", "RemoteProtocolError", "APITimeoutError",
     "APIConnectionError", "RateLimitError"],
)
def test_retryable_exception_names_without_status_code(exc_name):
    exc_cls = type(exc_name, (Exception,), {})
    assert classify(exc_cls("boom")) is Verdict.RETRY


def test_unknown_error_defaults_to_terminal():
    assert classify(ValueError("a real bug")) is Verdict.TERMINAL


# --- Free-tier generation: 429 is routine, not exceptional ----------------
# Daleel's generation now runs on a free tier where hitting the per-minute
# quota is an ordinary condition. A rate limit misclassified as TERMINAL
# would mean a demo question fails outright instead of being retried, so
# both routes to that verdict are pinned down here.

def test_rate_limit_with_status_code_is_retryable():
    """The normal case: the SDK attaches status 429."""
    assert classify(_StatusError(429)) is Verdict.RETRY


def test_rate_limit_without_status_code_is_still_retryable():
    """
    The defensive case: a provider (or a compatibility shim in front of one)
    raises RateLimitError with no status attached. Matching the class name
    keeps it retryable anyway.
    """
    exc_cls = type("RateLimitError", (Exception,), {})
    assert classify(exc_cls("quota exceeded")) is Verdict.RETRY


def test_per_day_quota_exhaustion_is_terminal_not_retryable():
    """
    A per-DAY free-tier quota cannot be waited out with a 15-second backoff.
    Measured: gemini-3.5-flash allows 20 requests per day, and hitting it
    sent the stage runner through all four retries for 77 seconds before
    failing anyway. Fail fast so a demo can switch to its fallback.
    """
    exc = _StatusError(429)
    exc.args = (
        "Quota exceeded for metric: generate_content_free_tier_requests, "
        "quotaId: 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
        "quotaValue: '20'",
    )
    assert classify(exc) is Verdict.TERMINAL


def test_per_minute_quota_is_still_retryable():
    """The distinction matters: a per-minute limit IS worth backing off."""
    exc = _StatusError(429)
    exc.args = ("Quota exceeded, quotaId: 'GenerateRequestsPerMinutePerProject-FreeTier'",)
    assert classify(exc) is Verdict.RETRY


def test_openai_sdk_rate_limit_error_is_retryable():
    """
    Same check against the REAL exception class from the installed SDK
    rather than a stand-in, so an upstream rename cannot pass silently.
    """
    import httpx
    from openai import RateLimitError

    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request)
    exc = RateLimitError("rate limited", response=response, body=None)
    assert classify(exc) is Verdict.RETRY
