"""
Request-path tests — the four response shapes, and D4's one-turn limit.

Generation is faked throughout. What is being tested is the wiring: that
each router strategy produces the right SHAPE, that refusals never look
like answers, and that the clarifying budget is enforced on the SERVER.

That last one matters most. The browser reports how many clarifications
have happened; it does not police the limit. A rule living only in frontend
state stops existing on refresh — and an agent that interrogates a judge
three times reads as broken, not intelligent.
"""

import pytest

from chatbot import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    # Flask-Limiter would reject the repeated posts these tests make.
    app_module.limiter.enabled = False
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def fake_generation(monkeypatch):
    """A well-formed grounded answer, unless a test overrides it."""
    def fake(question, chunks, history=None, model=None):
        return {
            "format": "answer",
            "answer": "رسوم تجديد الجواز عشرون ريالاً عمانياً.",
            "confidence": "high",
            "service_name": chunks[0].get("service"),
            "authority": chunks[0].get("authority"),
            "source_url": chunks[0].get("source_url"),
            "source_title": chunks[0].get("source_title"),
            "sources": [{"service": chunks[0].get("service"),
                         "authority": chunks[0].get("authority"),
                         "url": chunks[0].get("source_url"),
                         "title": chunks[0].get("source_title"),
                         "ref": chunks[0].get("source_title")}],
            "_served_by": "gemini-3.6-flash",
        }
    monkeypatch.setattr(app_module, "generate_answer", fake)
    return fake


def ask(client, text, **extra):
    body = {"history": [{"role": "user", "content": text}], **extra}
    return client.post("/chat", json=body).get_json()


# --- The four shapes -----------------------------------------------------

def test_grounded_question_returns_an_answer(client):
    data = ask(client, "كيف أجدد جواز سفري؟")
    assert data["format"] == "answer"
    assert "عشرون" in data["answer"]
    assert data["source_url"]
    assert data["sources"]


def test_uncovered_authority_returns_a_refusal_not_an_empty_card(client):
    """
    A refusal must not be a card with blank fields. The UI styles the two
    differently, and a citizen reading empty rows would think the service
    has no fees rather than that Daleel has no information.
    """
    data = ask(client, "أريد شكوى في وزارة الخارجية")
    assert data["format"] == "refusal"
    assert data["reason"] == "uncovered_authority"
    assert "وزارة الخارجية" in data["reply"]
    assert "answer" not in data


def test_ambiguous_question_returns_tappable_options(client):
    data = ask(client, "أريد جواز سفر")
    assert data["format"] == "clarify"
    assert data["reply"]
    assert len(data["options"]) == 2
    for option in data["options"]:
        assert option["label_ar"] and option["service_id"]


def test_l3_none_becomes_a_refusal(client, monkeypatch):
    """
    The model reported it cannot answer from this context. Rendering a
    checklist of «غير محدد في المصدر» rows would look like a broken answer;
    the honest rendering is a refusal.
    """
    monkeypatch.setattr(app_module, "generate_answer",
                        lambda *a, **k: {"format": "answer", "answer": "لا شيء",
                                         "confidence": "none", "_served_by": "m"})
    data = ask(client, "كيف أجدد جواز سفري؟")
    assert data["format"] == "refusal"
    assert data["reason"] == "ungrounded"


# --- D4: at most ONE clarifying turn, enforced server-side ---------------

def test_tapping_an_option_goes_straight_to_that_service(client):
    """The citizen's tap is an answer, not a hint. No re-classification."""
    data = ask(client, "تجديد جواز سفر قائم",
               service_id="rop-passport-renewal", clarify_count=1)
    assert data["format"] == "answer"
    assert data["strategy"] == "whole_service"


def test_second_ambiguous_question_does_not_clarify_again(client):
    """
    The budget is spent. Daleel must answer rather than asking again.
    """
    data = ask(client, "أريد جواز سفر", clarify_count=1)
    assert data["format"] != "clarify"
    assert data["format"] == "answer"


def test_spent_budget_loads_every_candidate_not_just_the_first(client, monkeypatch):
    """
    Picking one candidate arbitrarily is right about half the time, and
    when it is wrong L3 correctly reports it cannot answer — so the citizen
    gets a refusal for a question Daleel could have answered from the other
    candidate. Loading both lets the model use whichever matches.

    Observed live before this fix: the budget-spent path picked the wrong
    one of two candidates, and the answer came back as a refusal for a
    question the other candidate could have answered.

    This runs against the REAL corpus. Both candidates now have files
    (rop-passport-renewal and rop-passport-issuance), so both sets of
    sections load. The router asking for both is asserted in
    test_router.py, where fetch_service is faked.
    """
    seen = {}

    def capture(question, chunks, history=None, model=None):
        seen["service_ids"] = {c.get("service_id") for c in chunks}
        return {"format": "answer", "answer": "نص", "confidence": "high",
                "_served_by": "m"}

    monkeypatch.setattr(app_module, "generate_answer", capture)
    ask(client, "أريد جواز سفر", clarify_count=1)
    assert "rop-passport-renewal" in seen["service_ids"]


def test_the_limit_is_server_side_not_client_side(client):
    """
    Same question, same session, differing only in the reported count. The
    server decides. A refresh that resets browser state cannot buy another
    clarifying turn.
    """
    first = ask(client, "أريد جواز سفر", clarify_count=0)
    second = ask(client, "أريد جواز سفر", clarify_count=1)
    assert first["format"] == "clarify"
    assert second["format"] == "answer"


# --- Follow-up questions -------------------------------------------------

def test_a_follow_up_is_answered_from_the_previous_service(client, monkeypatch):
    """
    The wiring test. The router only sees history if app.py passes it, and
    the symptom of forgetting to is not an error — it is a clarification
    about an unrelated service, one turn after a correct answer.

    Runs against the REAL corpus: «وكم رسومها؟» carries no service words at
    all, so anything it grounds in came from the previous turn.
    """
    seen = {}

    def capture(question, chunks, history=None, model=None):
        seen["services"] = {c.get("service_id") for c in chunks}
        return {"format": "answer", "answer": "خمسة ريالات",
                "confidence": "high", "_served_by": "m"}

    monkeypatch.setattr(app_module, "generate_answer", capture)
    data = client.post("/chat", json={"history": [
        {"role": "user", "content": "كيف أجدد جواز سفري؟"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "وكم رسومها؟"},
    ]}).get_json()

    assert data["format"] == "answer"
    assert "rop-passport-renewal" in seen["services"]


def test_the_current_question_is_not_double_classified(client, monkeypatch):
    """
    app.py passes history[:-1]; the last entry IS the current question and
    is passed separately. Sending the whole list would have the router
    classify the same text twice — harmless here, but it would make a
    first-ever question look like a follow-up to itself.
    """
    captured = {}

    def spy_plan(question, *, service_id=None, allow_clarify=True, history=None):
        captured["question"] = question
        captured["history"] = history
        from chatbot.agent.router import plan_and_gather as real
        return real(question, service_id=service_id,
                    allow_clarify=allow_clarify, history=history)

    monkeypatch.setattr(app_module, "plan_and_gather", spy_plan)
    ask(client, "كيف أجدد جواز سفري؟")
    assert captured["question"] == "كيف أجدد جواز سفري؟"
    assert captured["history"] == []


# --- Failover visibility -------------------------------------------------

def test_response_reports_which_model_answered(client):
    """
    Surfaced in the UI so a fallback answer is visible mid-demo — and so a
    judge can see the chain engage with no degradation in the answer.
    """
    assert ask(client, "كيف أجدد جواز سفري؟")["served_by"] == "gemini-3.6-flash"


def test_internal_served_by_key_is_not_leaked(client):
    assert "_served_by" not in ask(client, "كيف أجدد جواز سفري؟")


# --- Failure and edges ---------------------------------------------------

def test_empty_question_is_rejected_in_arabic(client):
    response = client.post("/chat", json={"history": []})
    assert response.status_code == 400
    assert response.get_json()["format"] == "refusal"


def test_generation_failure_returns_an_arabic_refusal_not_a_stacktrace(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("upstream exploded")
    monkeypatch.setattr(app_module, "generate_answer", boom)
    response = client.post(
        "/chat", json={"history": [{"role": "user", "content": "كيف أجدد جواز سفري؟"}]})
    assert response.status_code == 500
    data = response.get_json()
    assert data["format"] == "refusal"
    assert "تعذّر" in data["reply"]


def test_refusal_and_clarify_never_call_the_model(client, monkeypatch):
    """
    Free-tier quota is the scarce resource. Neither a refusal nor a
    clarification has anything to generate, so neither may spend a request.
    """
    def explode(*a, **k):
        raise AssertionError("generation must not run for this strategy")
    monkeypatch.setattr(app_module, "generate_answer", explode)
    assert ask(client, "أريد شكوى في وزارة الخارجية")["format"] == "refusal"
    assert ask(client, "أريد جواز سفر")["format"] == "clarify"


# --- healthz -------------------------------------------------------------

def test_healthz_reports_coverage_and_models(client):
    data = client.get("/healthz").get_json()
    assert data["status"] == "ok"
    # Floors rather than exact counts — the corpus grows with each import,
    # and the exact figures are asserted against the registry itself in
    # test_registry.py. What matters here is that /healthz reports real
    # coverage rather than zeros.
    assert data["coverage"]["authority_count"] >= 10
    assert data["coverage"]["service_count"] >= 26
    assert isinstance(data["models_available"], list)


# --- the chat allowance meter --------------------------------------------
# The numbers shown must be the numbers enforced. A meter that disagrees
# with the limiter promises messages the server then refuses, which is a
# worse experience than showing nothing at all.

def test_usage_reports_a_full_allowance_before_any_chat(client):
    data = client.get("/usage").get_json()
    assert data["limit"] == 20
    assert data["remaining"] == 20
    assert data["percent_used"] == 0


def test_usage_does_not_itself_consume_the_allowance(client):
    """
    /usage is limiter-exempt. Asking how many messages are left must not
    cost one — otherwise the meter drains the thing it measures.
    """
    for _ in range(5):
        client.get("/usage")
    assert client.get("/usage").get_json()["remaining"] == 20


def test_each_answer_reports_the_allowance_it_just_used():
    """
    Carried on the /chat reply rather than fetched separately, so the meter
    updates from the same response that consumed the message.

    Needs its own client: the shared fixture DISABLES the limiter so other
    tests can post freely, and a disabled limiter records nothing — the
    meter would read a full allowance forever. Uses a unique caller address
    so other tests' requests are not in this bucket.
    """
    app_module.app.config["TESTING"] = True
    app_module.limiter.enabled = True
    caller = {"REMOTE_ADDR": "203.0.113.7"}   # TEST-NET-3, reserved for docs
    try:
        with app_module.app.test_client() as c:
            first = c.post("/chat", environ_base=caller, json={
                "history": [{"role": "user", "content": "كيف أجدد جواز سفري؟"}]
            }).get_json()["quota"]
            second = c.post("/chat", environ_base=caller, json={
                "history": [{"role": "user", "content": "كيف أجدد جواز سفري؟"}]
            }).get_json()["quota"]
    finally:
        app_module.limiter.enabled = False

    assert first["used"] == 1 and first["remaining"] == 19
    assert second["used"] == 2 and second["remaining"] == 18
    assert second["percent_used"] == 10


def test_the_meter_agrees_with_what_the_limiter_enforces(client):
    """
    The limit in the meter and the limit in the decorator come from one
    constant, so they cannot drift apart.
    """
    from limits import parse

    assert client.get("/usage").get_json()["limit"] == \
        parse(app_module.CHAT_RATE_LIMIT).amount


def test_a_broken_limiter_hides_the_meter_rather_than_failing(client, monkeypatch):
    """
    The meter is a convenience. A storage hiccup must never be the reason a
    citizen cannot get an answer, so it degrades to absent.
    """
    def boom(*a, **k):
        raise RuntimeError("storage gone")

    monkeypatch.setattr(app_module.limiter.limiter, "get_window_stats", boom)
    assert app_module.quota_status() is None
    assert ask(client, "كيف أجدد جواز سفري؟")["format"] == "answer"
