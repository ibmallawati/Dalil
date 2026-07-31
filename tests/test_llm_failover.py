"""
Failover tests — LLM_MODEL as an ordered chain.

All fake. The behaviour being tested is what happens when a daily quota is
exhausted, and the only way to reach that state for real is to exhaust a
real quota — which is precisely the thing this feature exists to survive.

The distinction under test is narrow and load-bearing:

  per-DAY 429   -> this model is finished until midnight Pacific. Fail over.
  per-MINUTE 429 -> transient. Do NOT fail over; re-raise so the Phase 5
                    stage runner backs off and retries the SAME model.

Getting that backwards means either burning the whole chain on a momentary
rate limit, or stalling on a quota that cannot recover.
"""

import pytest

from chatbot import llm


class FakeCompletion:
    def __init__(self, model):
        self.model = model
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": "مرحبا"})(),
            "finish_reason": "stop",
        })()]
        self.usage = type("U", (), {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        })()


class QuotaError(Exception):
    def __init__(self, quota_id):
        super().__init__(
            f"Error code: 429 - quota exceeded, quotaId: '{quota_id}'")
        self.status_code = 429


PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"

CHAIN = ("model-a", "model-b", "model-c")


@pytest.fixture(autouse=True)
def chain(monkeypatch):
    """A three-model chain, and a clean _unavailable map per test."""
    import dataclasses
    monkeypatch.setattr(llm, "settings",
                        dataclasses.replace(llm.settings, llm_models=CHAIN))
    monkeypatch.setattr(llm, "_unavailable", {})
    monkeypatch.setattr(llm, "_send_reasoning_effort", False)


def install(monkeypatch, behaviour):
    """behaviour: model name -> Exception to raise, or None to succeed."""
    calls = []

    def fake_create(**params):
        model = params["model"]
        calls.append(model)
        outcome = behaviour.get(model)
        if outcome is not None:
            raise outcome
        return FakeCompletion(model)

    monkeypatch.setattr(llm.client.chat.completions, "create", fake_create)
    return calls


# --- Per-day exhaustion fails over --------------------------------------

def test_exhausted_model_falls_through_to_the_next(monkeypatch):
    calls = install(monkeypatch, {"model-a": QuotaError(PER_DAY)})
    completion = llm.chat([{"role": "user", "content": "س"}])
    assert calls == ["model-a", "model-b"]
    assert completion.model == "model-b"


def test_failover_skips_every_exhausted_model(monkeypatch):
    calls = install(monkeypatch, {
        "model-a": QuotaError(PER_DAY),
        "model-b": QuotaError(PER_DAY),
    })
    completion = llm.chat([{"role": "user", "content": "س"}])
    assert calls == ["model-a", "model-b", "model-c"]
    assert completion.model == "model-c"


def test_exhaustion_is_remembered_for_the_process(monkeypatch):
    """
    Discovering a model is spent costs one wasted request per model per
    run, not one per call. On stage that is the difference between a single
    slow answer and every answer being slow.
    """
    calls = install(monkeypatch, {"model-a": QuotaError(PER_DAY)})
    llm.chat([{"role": "user", "content": "س"}])
    llm.chat([{"role": "user", "content": "س"}])
    assert calls == ["model-a", "model-b", "model-b"]
    assert "model-a" not in llm.available_models()


def test_raises_only_when_the_whole_chain_is_spent(monkeypatch):
    install(monkeypatch, {m: QuotaError(PER_DAY) for m in CHAIN})
    with pytest.raises(Exception):
        llm.chat([{"role": "user", "content": "س"}])
    assert llm.available_models() == []


def test_error_names_every_exhausted_model(monkeypatch):
    install(monkeypatch, {m: QuotaError(PER_DAY) for m in CHAIN})
    with pytest.raises(Exception):
        llm.chat([{"role": "user", "content": "س"}])
    with pytest.raises(RuntimeError, match="unavailable"):
        llm.chat([{"role": "user", "content": "س"}])


# --- Per-minute rate limits must NOT fail over ---------------------------

def test_per_minute_rate_limit_does_not_burn_the_chain(monkeypatch):
    """
    A momentary rate limit is the stage runner's job. Failing over would
    spend a second model's quota on a problem that fixes itself in seconds.
    """
    calls = install(monkeypatch, {"model-a": QuotaError(PER_MINUTE)})
    with pytest.raises(QuotaError):
        llm.chat([{"role": "user", "content": "س"}])
    assert calls == ["model-a"]
    assert llm.available_models() == list(CHAIN)


def test_transient_server_error_does_not_fail_over(monkeypatch):
    class ServerError(Exception):
        status_code = 503

    calls = install(monkeypatch, {"model-a": ServerError("overloaded")})
    with pytest.raises(ServerError):
        llm.chat([{"role": "user", "content": "س"}])
    assert calls == ["model-a"]


# --- Explicit model overrides bypass the chain ---------------------------

def test_explicit_model_argument_is_not_failed_over(monkeypatch):
    """An experiment pinned to one model must not silently use another."""
    calls = install(monkeypatch, {"model-a": QuotaError(PER_DAY)})
    with pytest.raises(QuotaError):
        llm.chat([{"role": "user", "content": "س"}], model="model-a")
    assert calls == ["model-a"]


# --- Reporting -----------------------------------------------------------

def test_usage_reports_which_model_actually_answered(monkeypatch):
    install(monkeypatch, {"model-a": QuotaError(PER_DAY)})
    completion = llm.chat([{"role": "user", "content": "س"}])
    usage = llm.usage_of(completion)
    assert usage["served_by"] == "model-b"
    assert usage["reasoning_tokens"] == 0
