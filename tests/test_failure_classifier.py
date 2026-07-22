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
     "ConnectError", "RemoteProtocolError", "APITimeoutError", "APIConnectionError"],
)
def test_retryable_exception_names_without_status_code(exc_name):
    exc_cls = type(exc_name, (Exception,), {})
    assert classify(exc_cls("boom")) is Verdict.RETRY


def test_unknown_error_defaults_to_terminal():
    assert classify(ValueError("a real bug")) is Verdict.TERMINAL
