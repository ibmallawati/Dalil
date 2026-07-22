"""
Tests for chatbot.reliability.stage_runner - the retry/backoff worker.

Uses the `sleeper` injection point so retries don't actually wait.
"""

from src.chatbot.reliability.stage_runner import RetryPolicy, StageStatus, run_stage


def no_sleep(_seconds):
    pass


def test_first_try_success_is_ok():
    result = run_stage("demo", lambda: "value", sleeper=no_sleep)
    assert result.status is StageStatus.OK
    assert result.attempts == 1
    assert result.payload == "value"
    assert result.delays_used == []


def test_retry_then_success_is_retried_ok():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return "recovered"

    policy = RetryPolicy(delays=(1, 2, 4))
    result = run_stage("demo", flaky, policy=policy, sleeper=no_sleep)

    assert result.status is StageStatus.RETRIED_OK
    assert result.attempts == 3
    assert result.payload == "recovered"
    assert result.delays_used == [1, 2]


def test_retries_exhausted_is_failed():
    def always_fails():
        raise ConnectionError("still broken")

    policy = RetryPolicy(delays=(1, 2))
    result = run_stage("demo", always_fails, policy=policy, sleeper=no_sleep)

    assert result.status is StageStatus.FAILED
    assert result.attempts == 3  # 1 initial + 2 retries
    assert result.delays_used == [1, 2]
    assert "still broken" in result.error


def test_terminal_error_fails_without_retrying():
    calls = {"n": 0}

    def bad_request():
        calls["n"] += 1
        exc = Exception("bad request")
        exc.status_code = 400
        raise exc

    policy = RetryPolicy(delays=(1, 2, 4))
    result = run_stage("demo", bad_request, policy=policy, sleeper=no_sleep)

    assert result.status is StageStatus.FAILED
    assert calls["n"] == 1  # no retries spent on a terminal error


def test_is_empty_reports_empty_not_a_failure():
    result = run_stage("demo", lambda: [], sleeper=no_sleep, is_empty=lambda res: len(res) == 0)
    assert result.status is StageStatus.EMPTY
    assert result.payload == []
