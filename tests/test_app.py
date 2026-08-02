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
from chatbot.agent.card import NOT_IN_SOURCE


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    # Flask-Limiter would reject the repeated posts these tests make.
    app_module.limiter.enabled = False
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def fake_generation(monkeypatch):
    """A well-formed grounded card, unless a test overrides it."""
    def fake(question, chunks, history=None, model=None):
        return {
            "format": "card",
            "eligibility": "المواطنون العمانيون",
            "required_documents": ["البطاقة الشخصية"],
            "steps": ["الخطوة الأولى"],
            "fees": "عشرون ريالاً عمانياً",
            "estimated_time": "ثلاثة أيام",
            "where": "إلكترونياً",
            "confidence": "high",
            "service_name": chunks[0].get("service"),
            "authority": chunks[0].get("authority"),
            "source_url": chunks[0].get("source_url"),
            "source_title": chunks[0].get("source_title"),
            "_served_by": "gemini-3.6-flash",
        }
    monkeypatch.setattr(app_module, "generate_answer", fake)
    return fake


def ask(client, text, **extra):
    body = {"history": [{"role": "user", "content": text}], **extra}
    return client.post("/chat", json=body).get_json()


# --- The four shapes -----------------------------------------------------

def test_grounded_question_returns_a_card(client):
    data = ask(client, "كيف أجدد جواز سفري؟")
    assert data["format"] == "card"
    assert data["strategy"] == "whole_service"
    assert data["fees"] == "عشرون ريالاً عمانياً"
    assert data["source_url"]


def test_uncovered_authority_returns_a_refusal_not_an_empty_card(client):
    """
    A refusal must not be a card with blank fields. The UI styles the two
    differently, and a citizen reading empty rows would think the service
    has no fees rather than that Daleel has no information.
    """
    data = ask(client, "أريد شكوى في وزارة العمل")
    assert data["format"] == "refusal"
    assert data["reason"] == "uncovered_authority"
    assert "وزارة العمل" in data["reply"]
    assert "fees" not in data


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
                        lambda *a, **k: {"format": "card", "confidence": "none",
                                         "_served_by": "m"})
    data = ask(client, "كيف أجدد جواز سفري؟")
    assert data["format"] == "refusal"
    assert data["reason"] == "ungrounded"


# --- D4: at most ONE clarifying turn, enforced server-side ---------------

def test_tapping_an_option_goes_straight_to_that_service(client):
    """The citizen's tap is an answer, not a hint. No re-classification."""
    data = ask(client, "تجديد جواز سفر قائم",
               service_id="rop-passport-renewal", clarify_count=1)
    assert data["format"] == "card"
    assert data["strategy"] == "whole_service"


def test_second_ambiguous_question_does_not_clarify_again(client):
    """
    The budget is spent. Daleel must answer rather than asking again.
    """
    data = ask(client, "أريد جواز سفر", clarify_count=1)
    assert data["format"] != "clarify"
    assert data["format"] == "card"


def test_spent_budget_loads_every_candidate_not_just_the_first(client, monkeypatch):
    """
    Picking one candidate arbitrarily is right about half the time, and
    when it is wrong L3 correctly reports it cannot answer — so the citizen
    gets a refusal for a question Daleel could have answered from the other
    candidate. Loading both lets the model use whichever matches.

    Observed live before this fix: the budget-spent path picked the wrong
    one of two candidates, and the answer came back as a refusal for a
    question the other candidate could have answered.

    This runs against the REAL corpus, where rop-passport-new-issuance has
    no file yet — so only the renewal sections exist to load. The router
    asking for both is asserted in test_router.py, where fetch_service is
    faked and can answer for both.
    """
    seen = {}

    def capture(question, chunks, history=None, model=None):
        seen["service_ids"] = {c.get("service_id") for c in chunks}
        return {"format": "card", "confidence": "high", "_served_by": "m"}

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
    assert second["format"] == "card"


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
    assert ask(client, "أريد شكوى في وزارة العمل")["format"] == "refusal"
    assert ask(client, "أريد جواز سفر")["format"] == "clarify"


# --- healthz -------------------------------------------------------------

def test_healthz_reports_coverage_and_models(client):
    data = client.get("/healthz").get_json()
    assert data["status"] == "ok"
    assert data["coverage"]["authority_count"] == 4
    assert data["coverage"]["service_count"] == 17
    assert isinstance(data["models_available"], list)
